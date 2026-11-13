"""Read-only Slack for Friday, with Aman's user token (xoxp-…, SLACK_USER_TOKEN in ~/.friday/.env).

Only read endpoints are called: auth.test, users.conversations, conversations.info/history,
search.messages, users.info. Nothing here can post, react or mark anything read.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

from .paths import FRIDAY_HOME

API = "https://slack.com/api/"


class SlackError(RuntimeError):
    pass


def _token() -> Optional[str]:
    if os.environ.get("SLACK_USER_TOKEN"):
        return os.environ["SLACK_USER_TOKEN"]
    env = FRIDAY_HOME / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SLACK_USER_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


class Slack:
    def __init__(self, token: Optional[str] = None):
        self.token = token or _token()
        if not self.token:
            raise SlackError("no Slack token: add SLACK_USER_TOKEN=xoxp-… to ~/.friday/.env")
        self._users: dict[str, str] = {}
        self._me: Optional[dict] = None
        self._channels: Optional[dict[str, dict]] = None

    def call(self, method: str, **params) -> dict:
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(API + method, data=data, headers={"Authorization": f"Bearer {self.token}"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(float(e.headers.get("Retry-After", "2")))
                    continue
                raise SlackError(f"{method}: HTTP {e.code}") from e
            if not body.get("ok"):
                raise SlackError(f"{method}: {body.get('error')}")
            return body
        raise SlackError(f"{method}: rate limited")

    # --- names -------------------------------------------------------------------------------

    def me(self) -> dict:
        if self._me is None:
            self._me = self.call("auth.test")
        return self._me

    def user(self, uid: str) -> str:
        if uid not in self._users:
            try:
                u = self.call("users.info", user=uid)["user"]
                self._users[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name") or uid
            except SlackError:
                self._users[uid] = uid
        return self._users[uid]

    def channels(self) -> dict[str, dict]:
        """Conversations Aman is a member of, by id."""
        if self._channels is None:
            out, cursor = {}, None
            while True:
                r = self.call("users.conversations", types="public_channel,private_channel,mpim,im",
                              exclude_archived="true", limit=200, cursor=cursor)
                for c in r["channels"]:
                    out[c["id"]] = c
                cursor = (r.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            self._channels = out
        return self._channels

    def channel_name(self, c: dict) -> str:
        if c.get("is_im"):
            return f"DM with {self.user(c.get('user', '?'))}"
        if c.get("is_mpim"):
            return "group DM " + c.get("name", "").replace("mpdm-", "").replace("--", ", ").rstrip("-1")
        return "#" + c.get("name", c["id"])

    def clean(self, text: str) -> str:
        text = re.sub(r"<@([A-Z0-9]+)>", lambda m: "@" + self.user(m.group(1)), text or "")
        text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
        text = re.sub(r"<(https?://[^>]+)>", r"(link)", text)
        return " ".join(text.split())

    def _msg(self, m: dict, limit: int = 400) -> dict:
        who = self.user(m["user"]) if m.get("user") else (m.get("username") or m.get("bot_profile", {}).get("name") or "bot")
        t = time.strftime("%a %H:%M", time.localtime(float(m.get("ts", 0))))
        text = self.clean(m.get("text", ""))
        return {"when": t, "from": who, "text": text if len(text) <= limit else text[: limit - 1] + "…",
                **({"replies": m["reply_count"]} if m.get("reply_count") else {})}

    # --- what Friday asks for ------------------------------------------------------------------

    def catchup(self, hours: float = 12, max_channels: int = 12, per_channel: int = 8) -> dict:
        """Mentions of Aman, unread DMs, and the most active channels he's in, over the last ``hours``."""
        me = self.me()
        since = time.time() - hours * 3600
        day = time.strftime("%Y-%m-%d", time.localtime(since - 86400))
        mentions = []
        try:
            r = self.call("search.messages", query=f"<@{me['user_id']}> after:{day}", sort="timestamp", count=30)
            for m in r["messages"]["matches"]:
                if float(m["ts"]) >= since:
                    mentions.append({"where": "#" + m.get("channel", {}).get("name", "?"), **self._msg(m)})
        except SlackError as e:
            mentions = [{"error": str(e)}]

        chans = self.channels()
        unread_dms = []   # DMs where someone else wrote within the window (open DMs only; one call each)
        for c in [c for c in chans.values() if (c.get("is_im") or c.get("is_mpim")) and c.get("is_open", True)]:
            try:
                hist = self.call("conversations.history", channel=c["id"], oldest=str(since), limit=per_channel)
            except SlackError:      # deactivated users, Slack Connect DMs, etc.
                continue
            msgs = [m for m in hist.get("messages", []) if m.get("user") and m.get("user") != me["user_id"]]
            if msgs:
                unread_dms.append({"conversation": self.channel_name(c), "messages_from_them": len(msgs),
                                   "messages": [self._msg(m) for m in reversed(msgs)]})

        activity: dict[str, list] = {}
        try:
            r = self.call("search.messages", query=f"after:{day}", sort="timestamp", count=100)
            for m in r["messages"]["matches"]:
                cid = (m.get("channel") or {}).get("id")
                if cid in chans and not chans[cid].get("is_im") and not chans[cid].get("is_mpim") and float(m["ts"]) >= since:
                    activity.setdefault(cid, []).append(m)
        except SlackError:
            pass
        busiest = sorted(activity.items(), key=lambda kv: len(kv[1]), reverse=True)[:max_channels]
        channels_out = [{"channel": self.channel_name(chans[cid]), "messages_in_window": len(ms),
                         "recent": [self._msg(m, 300) for m in reversed(ms[:per_channel])]} for cid, ms in busiest]
        return {"you": me.get("user"), "workspace": me.get("team"), "window_hours": hours,
                "mentions": mentions, "unread_dms": unread_dms, "busiest_channels": channels_out}

    def read(self, channel: str, hours: float = 24, limit: int = 40) -> dict:
        want = channel.lstrip("#").lower()
        chans = self.channels()
        match = next((c for c in chans.values() if c.get("name", "").lower() == want), None) or next(
            (c for c in chans.values() if want in c.get("name", "").lower()), None)
        if match is None:
            raise SlackError(f"no channel you're in matches {channel!r}")
        hist = self.call("conversations.history", channel=match["id"], oldest=str(time.time() - hours * 3600), limit=limit)
        return {"channel": self.channel_name(match), "messages": [self._msg(m, 2000) for m in reversed(hist.get("messages", []))]}

    def search(self, query: str, count: int = 20) -> dict:
        r = self.call("search.messages", query=query, sort="timestamp", count=count)
        return {"query": query, "total": r["messages"].get("total"),
                "matches": [{"where": "#" + m.get("channel", {}).get("name", "?"), **self._msg(m, 2000)}
                            for m in r["messages"]["matches"]]}
