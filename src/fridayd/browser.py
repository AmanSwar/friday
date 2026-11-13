"""The bridge to Aman's own Brave. The Friday extension (extensions/brave) connects out to this
WebSocket server; fridayd relays the agent's browser commands to it (POST /browser) and returns the
results. Pages are read inside his real browser, signed in as him, without copying any profile data.

Only a chrome-extension:// origin carrying the token in ~/.friday/state/brave-bridge-token is
accepted, so a web page (whose Origin the browser sets) or another extension can't attach."""

from __future__ import annotations

import http
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeout
from urllib.parse import parse_qs, urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from friday_core.paths import BRIDGE_PORT, bridge_token

log = logging.getLogger("fridayd.browser")
logging.getLogger("websockets").setLevel(logging.WARNING)   # one line per connection otherwise
EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")


class BridgeError(Exception):
    pass


class _Link:
    """One connected extension (a Brave profile). Several may be connected; the newest gets commands."""

    def __init__(self, conn: ServerConnection):
        self.conn = conn
        self.since = time.time()
        self.hello: dict = {}
        self.focus: dict | None = None          # the tab he pinned Friday to last ("this page")
        self.group: list[dict] = []             # every tab in the Friday group: all Friday can see
        self.pending: dict[str, Future] = {}
        self.send_lock = threading.Lock()


class BraveBridge(threading.Thread):
    def __init__(self, port: int = BRIDGE_PORT):
        super().__init__(name="brave-bridge", daemon=True)
        self.port = port
        self.token = bridge_token()
        self._links: list[_Link] = []          # oldest first
        self._lock = threading.Lock()
        self.on_event = None                    # fridayd's handler for things the extension notices (deep_read)
        self.on_pin = None                      # he pinned Friday to a tab (focus dict): fridayd offers help

    # --- server ---------------------------------------------------------------------------------

    def run(self) -> None:
        while True:
            try:
                with serve(self._on_connection, "127.0.0.1", self.port, origins=[EXTENSION_ORIGIN],
                           process_request=self._check_token, compression=None, max_size=16 << 20) as server:
                    log.info("Brave bridge listening on 127.0.0.1:%d", self.port)
                    server.serve_forever()
            except OSError as e:
                log.warning("Brave bridge could not listen (%s); retrying", e)
                time.sleep(10)

    def _check_token(self, connection: ServerConnection, request):
        token = (parse_qs(urlparse(request.path).query).get("token") or [""])[0]
        if token != self.token:
            log.warning("Brave bridge: rejected a connection without the right token")
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        return None

    def _on_connection(self, conn: ServerConnection) -> None:   # not _handle: Thread uses that name
        link = _Link(conn)
        with self._lock:
            self._links.append(link)
            n = len(self._links)
        log.info("Brave extension connected (%d connected)", n)
        try:
            for raw in conn:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "hello":
                    link.hello = {k: msg.get(k) for k in ("version", "browser", "files")}
                    link.focus, link.group = msg.get("focus"), msg.get("group") or []
                elif kind == "event" and msg.get("kind") == "focus":
                    before = [t.get("id") for t in link.group]
                    old_at = (link.focus or {}).get("at")
                    link.focus, link.group = msg.get("focus"), msg.get("group") or []
                    f = link.focus or {}
                    if f.get("by") == "pin" and f.get("at") != old_at and self.on_pin is not None:
                        threading.Thread(target=self.on_pin, args=(dict(f),), name="brave-pin", daemon=True).start()
                    if [t.get("id") for t in link.group] != before:
                        log.info("Friday's group: %d tab(s); pinned: %s", len(link.group),
                                 (link.focus or {}).get("title") or "none")
                elif kind == "event" and self.on_event is not None:
                    threading.Thread(target=self.on_event, args=(msg,), name="brave-event", daemon=True).start()
                elif kind == "reply":
                    with self._lock:
                        fut = link.pending.pop(msg.get("id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except ConnectionClosed:
            pass
        finally:
            with self._lock:
                if link in self._links:
                    self._links.remove(link)
                pending, link.pending = link.pending, {}
                n = len(self._links)
            for fut in pending.values():
                if not fut.done():
                    fut.set_exception(BridgeError("Brave disconnected"))
            log.info("Brave extension disconnected (%d connected)", n)

    # --- client side ----------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self._links)

    def status(self) -> dict:
        with self._lock:
            link = self._links[-1] if self._links else None
            count = len(self._links)
        if link is None:
            return {"connected": False}
        return {"connected": True, "since": link.since, "connections": count, **link.hello,
                "focus": {k: (link.focus or {}).get(k) for k in ("id", "title", "url")} if link.focus else None,
                "group": len(link.group)}

    def focus(self) -> dict | None:
        with self._lock:
            return self._links[-1].focus if self._links else None

    def focus_note(self) -> str:
        """One line for each voice turn: the tabs Aman shared (the Friday group), so "this page" needs no lookup."""
        with self._lock:
            link = self._links[-1] if self._links else None
            focus, group = (link.focus, list(link.group)) if link else (None, [])
        if not group:
            return ""
        def short(t: dict) -> str:
            title = str(t.get("title") or t.get("url") or "")[:80]
            return f"\"{title}\" (tab_id {t.get('id')})"
        his = [t for t in group if not t.get("friday")]
        fridays = [t for t in group if t.get("friday")]
        parts = []
        if focus and focus.get("title"):
            parts.append(f"his pinned tab \"{focus['title'][:80]}\" ({focus.get('url', '')}), tab_id {focus.get('id')}: "
                         "\"this\", \"this page\" and \"the tab\" mean it")
        others = [t for t in his if not focus or t.get("id") != focus.get("id")]
        if others:
            parts.append("also shared: " + ", ".join(short(t) for t in others[:5]))
        if fridays:
            parts.append("tabs you opened there: " + ", ".join(short(t) for t in fridays[:5]))
        return "[Aman's Friday group in Brave (the only tabs you can see): " + "; ".join(parts) + ".]"

    def call(self, action: str, params: dict, timeout: float = 45) -> dict:
        """Run one command in Brave (the newest connection); returns its result or raises BridgeError."""
        with self._lock:
            if not self._links:
                raise BridgeError("Brave isn't connected: Brave is closed, or the Friday extension isn't loaded.")
            link = self._links[-1]
            cid = uuid.uuid4().hex
            fut: Future = Future()
            link.pending[cid] = fut
        try:
            with link.send_lock:
                link.conn.send(json.dumps({"type": "call", "id": cid, "action": action, "params": params}))
            reply = fut.result(timeout=timeout)
        except FutureTimeout:
            raise BridgeError(f"Brave didn't answer {action} within {int(timeout)} s") from None
        except ConnectionClosed:
            raise BridgeError("Brave disconnected") from None
        finally:
            with self._lock:
                link.pending.pop(cid, None)
        if not reply.get("ok"):
            raise BridgeError(reply.get("error") or f"{action} failed")
        return reply.get("result") or {}
