"""FridayEars' view of Friday, pushed instead of polled.

``UIHub`` rebuilds the UI state ten times a second and sends it to every connected app only when it
changed (``GET /ui/stream``, server-sent events, one JSON object per ``data:`` line). The state holds
timestamps, not running counters, so nothing changes while nothing happens: an idle Friday sends only
a heartbeat comment every 5 s. History (the panel's Earlier list) is rebuilt once a second and sent as
its own message. Each message type has its own sequence number; the app ignores anything older than
what it already showed, so a late answer can never put back an old state.

    data: {"type": "state", "seq": 41, "boot": 1731400000.0, "voice": {...}, "turn": {...}, ...}
    data: {"type": "history", "seq": 7, "boot": ..., "items": [...]}
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable

log = logging.getLogger("fridayd.ui")


class UIHub(threading.Thread):
    def __init__(self, state: Callable[[], dict], history: Callable[[], list], tick_s: float = 0.1,
                 history_every_s: float = 1.0):
        super().__init__(name="ui-hub", daemon=True)
        self.build_state, self.build_history = state, history
        self.tick_s, self.history_every_s = tick_s, history_every_s
        self.boot = time.time()
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._dropped: set[int] = set()                # id(queue) of subscribers dropped for falling behind
        self._raw = {"state": "", "history": ""}      # last content (without seq), to detect changes
        self._msg = {"state": "", "history": ""}      # last full message, primed to new subscribers
        self._seq = {"state": 0, "history": 0}
        self._ready = threading.Event()

    # --- subscribers (one per /ui/stream connection) ------------------------------------------------

    def subscribe(self) -> queue.Queue:
        self._ready.wait(5)
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            for kind in ("state", "history"):
                if self._msg[kind]:
                    q.put_nowait(self._msg[kind])
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
            self._dropped.discard(id(q))

    def dropped(self, q: queue.Queue) -> bool:
        with self._lock:
            return id(q) in self._dropped

    def latest(self) -> dict:
        """For GET /ui (the app's fallback poll while the stream is down)."""
        self._ready.wait(5)
        with self._lock:
            return {k: json.loads(v) if v else None for k, v in self._msg.items()}

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._subs)

    # --- the loop ---------------------------------------------------------------------------------

    def _publish(self, kind: str, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
        if raw == self._raw[kind]:
            return
        with self._lock:
            self._raw[kind] = raw
            self._seq[kind] += 1
            msg = json.dumps({"type": kind, "seq": self._seq[kind], "boot": self.boot, **body},
                             ensure_ascii=False, default=str)
            self._msg[kind] = msg
            for q in list(self._subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:          # a stuck client: drop it; its stream ends and the app reconnects
                    self._subs.remove(q)
                    self._dropped.add(id(q))

    def run(self) -> None:
        last_history = 0.0
        while True:
            try:
                self._publish("state", self.build_state())
                if time.time() - last_history >= self.history_every_s:
                    last_history = time.time()
                    self._publish("history", {"items": self.build_history()})
                self._ready.set()
            except Exception:
                log.exception("building the UI state failed")
            time.sleep(self.tick_s)
