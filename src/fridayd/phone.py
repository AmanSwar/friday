"""Aman's iPhone and Apple Watch, through ntfy (the free ntfy app; Watch mirrors iPhone notifications).

- Out: alerts while he's away, and Claude Code permission prompts with Allow / Deny buttons, are
  published to his notify topic (``[phone] ntfy_topic``).
- In: fridayd follows his reply topic (``[phone] reply_topic``). The buttons post "allow <id>" /
  "deny <id>" there; anything else he types in the ntfy app is a question for Friday, answered back as a
  notification; "health {...}" lines (from an iPhone Shortcut reading Apple Health) are stored for Friday.

Both topics are long random names (an unguessable topic is the only protection on ntfy.sh, so treat them
like passwords; ``scripts/phone-setup.py`` makes them). Messages pass through ntfy.sh; self-hosting
ntfy (``ntfy_server``) keeps them on a machine Aman controls."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from friday_core.paths import STATE_DIR

log = logging.getLogger("fridayd.phone")
HEALTH = STATE_DIR / "health.jsonl"


class Phone(threading.Thread):
    def __init__(self, server: str, topic: str, reply_topic: str, token: str = "",
                 on_answer: Optional[Callable[[str, Optional[int]], bool]] = None,
                 on_question: Optional[Callable[[str], None]] = None, enabled: object = True):
        super().__init__(name="phone", daemon=True)
        self.server, self.topic, self.reply_topic, self.token = server.rstrip("/"), topic, reply_topic, token
        # [phone] enabled = false keeps the topics but sends nothing to ntfy (no phone subscribed yet)
        self.enabled = str(enabled).strip().lower() not in ("false", "0", "no", "off", "")
        self.on_answer, self.on_question = on_answer, on_question
        self.since = str(int(time.time()))

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.topic and self.reply_topic)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def notify(self, title: str, message: str, priority: int = 3, tags: Optional[list] = None,
               actions: Optional[list] = None) -> bool:
        if not self.configured:
            return False
        body = {"topic": self.topic, "title": title[:120], "message": message[:3500], "priority": priority}
        if tags:
            body["tags"] = tags
        if actions:
            body["actions"] = actions
        try:
            req = urllib.request.Request(self.server, data=json.dumps(body).encode(), headers=self._headers(), method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except (OSError, urllib.error.URLError) as e:
            log.warning("phone notification failed: %s", e)
            return False

    def ask_permission(self, rid: int, who: str, summary: str, detail: str) -> bool:
        """A permission prompt with buttons; tapping posts the answer to the reply topic."""
        url = f"{self.server}/{self.reply_topic}"
        actions = [{"action": "http", "label": label, "url": url, "method": "POST", "body": f"{decision} {rid}", "clear": True}
                   for label, decision in (("Allow", "allow"), ("Deny", "deny"))]
        return self.notify(f"{who} wants permission", f"{summary}\n\n{detail[:600]}", priority=5, tags=["lock"], actions=actions)

    # --- the reply topic ------------------------------------------------------------------------------

    def run(self) -> None:
        if not self.configured:
            return
        while True:
            try:
                self._follow()
            except (OSError, urllib.error.URLError, ValueError) as e:
                log.debug("reply topic unavailable (%s); retrying", e)
            time.sleep(10)

    def _follow(self) -> None:
        url = f"{self.server}/{self.reply_topic}/json?since={self.since}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=3600) as resp:
            log.info("following the phone reply topic")
            for raw in resp:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("event") != "message":
                    continue
                self.since = ev.get("id") or self.since
                self.handle(str(ev.get("message") or "").strip())

    def handle(self, text: str) -> None:
        if not text:
            return
        word, _, rest = text.partition(" ")
        if word.lower() in ("allow", "deny", "always") and self.on_answer:
            rid = int(rest) if rest.strip().isdigit() else None
            self.on_answer(word.lower(), rid)
            return
        if word.lower() == "health":
            try:
                data = json.loads(rest)
            except ValueError:
                data = {"raw": rest[:2000]}
            HEALTH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(HEALTH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps({"ts": time.time(), **(data if isinstance(data, dict) else {"data": data})}) + "\n").encode())
            finally:
                os.close(fd)
            log.info("health summary received (%d fields)", len(data) if isinstance(data, dict) else 1)
            return
        if self.on_question:
            self.on_question(text)


def latest_health(max_age_h: float = 36) -> Optional[dict]:
    """The newest health summary from his phone, if it's recent."""
    try:
        lines = HEALTH.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if time.time() - d.get("ts", 0) < max_age_h * 3600:
            return d
        return None
    return None
