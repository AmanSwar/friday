"""Messages arriving for Aman, turned into alerts: Signal (live, via the signal-cli daemon's event
stream) and Slack (polled). Also Aman's phone channel: what he writes to his Signal Note to Self goes
to Friday, and her answer comes back there."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from friday_core import signal as signal_api
from friday_core.paths import STATE_DIR

log = logging.getLogger("fridayd.inbox")
SIGNAL_INBOX = STATE_DIR / "signal-inbox.jsonl"


def _append_private(path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


class SignalWatch(threading.Thread):
    """Follows the signal-cli daemon's live events. Direct messages become alerts (a burst from one
    person is batched into one); group messages are only logged; Note to Self is the phone channel."""

    def __init__(self, on_message: Callable[[str, list[str]], None], on_note_to_self: Callable[[str], None],
                 batch_s: float = 6):
        super().__init__(name="signal", daemon=True)
        self.on_message, self.on_note_to_self, self.batch_s = on_message, on_note_to_self, batch_s
        self._pending: dict[str, list[str]] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._groups: dict[str, str] = {}
        self._lock = threading.Lock()

    def run(self) -> None:
        while True:
            try:
                self.listen()
            except (OSError, urllib.error.URLError, ValueError) as e:
                log.debug("signal events unavailable (%s); retrying", e)
            time.sleep(10)

    def listen(self) -> None:
        with urllib.request.urlopen(f"{signal_api.DAEMON}/api/v1/events", timeout=3600) as resp:
            log.info("listening to Signal")
            data: list[str] = []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    try:
                        self.on_event(json.loads("".join(data)))
                    except Exception:
                        log.exception("bad Signal event")
                    data = []

    def group_name(self, gid: str) -> str:
        if gid not in self._groups:
            try:
                self._groups = {g["id"]: g.get("name") or "a group" for g in signal_api.rpc("listGroups") or []}
            except (OSError, signal_api.SignalError):
                pass
        return self._groups.get(gid, "a group")

    def display(self, number: Optional[str], fallback: str) -> str:
        for c in signal_api.contacts().values():
            if number and c.get("signal") == number:
                return (c.get("display") or fallback).split()[0]
        return fallback

    def on_event(self, ev: dict) -> None:
        env = ev.get("envelope") or {}
        me = ev.get("account")
        dm = env.get("dataMessage") or {}
        sent = (env.get("syncMessage") or {}).get("sentMessage") or {}
        if dm.get("message"):
            number = env.get("sourceNumber")
            who = self.display(number, env.get("sourceName") or number or "Someone")
            gid = (dm.get("groupInfo") or {}).get("groupId")
            _append_private(SIGNAL_INBOX, {"ts": time.time(), "dir": "in", "from": who, "number": number,
                                           "group": self.group_name(gid) if gid else None, "text": dm["message"]})
            if not gid:
                self.batch(who, dm["message"])
        elif sent.get("message"):
            dest = sent.get("destinationNumber") or sent.get("destination")
            gid = (sent.get("groupInfo") or {}).get("groupId")
            if dest == me and not gid:              # written on his phone, to Note to Self: for Friday
                log.info("Note to Self from Aman's phone")
                self.on_note_to_self(sent["message"])
            else:
                _append_private(SIGNAL_INBOX, {"ts": time.time(), "dir": "out", "to": dest,
                                               "group": self.group_name(gid) if gid else None, "text": sent["message"]})

    def batch(self, who: str, text: str) -> None:
        with self._lock:
            self._pending.setdefault(who, []).append(text)
            if who in self._timers:
                self._timers[who].cancel()
            t = threading.Timer(self.batch_s, self.flush, args=(who,))
            t.daemon = True
            self._timers[who] = t
            t.start()

    def flush(self, who: str) -> None:
        with self._lock:
            texts = self._pending.pop(who, [])
            self._timers.pop(who, None)
        if texts:
            self.on_message(who, texts)


class SlackWatch(threading.Thread):
    """Every few minutes: new Slack mentions and DMs since the last look, as one short spoken heads-up."""

    def __init__(self, on_update: Callable[[list[dict]], None], every_s: float = 180):
        super().__init__(name="slack", daemon=True)
        self.on_update, self.every_s = on_update, every_s
        self.since = time.time()

    def run(self) -> None:
        from friday_core.slack import Slack, SlackError, _token
        while True:
            time.sleep(self.every_s)
            if not _token():
                continue
            start = time.time()
            try:
                c = Slack().catchup(hours=(start - self.since) / 3600 + 0.01, max_channels=0, per_channel=6)
            except (SlackError, OSError) as e:
                log.warning("slack check failed: %s", e)
                continue
            items = [{"kind": "mention", **m} for m in c.get("mentions", []) if "error" not in m]
            items += [{"kind": "dm", "where": d["conversation"], **m} for d in c.get("unread_dms", []) for m in d["messages"]]
            self.since = start
            if items:
                log.info("slack: %d new mentions/DMs", len(items))
                self.on_update(items)
