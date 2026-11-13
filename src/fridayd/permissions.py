"""Claude Code permission prompts answered by voice. The PermissionRequest hook (bin/friday-permission-hook)
asks fridayd and waits. For a session Friday supervises (a master or one of its workers), Friday says
what it wants ("The AWS master wants to run a command: install the vLLM nightly. Allow?"), FridayEars
listens for the answer, and "yes" / "no" / "always" settles it with no model call. With no answer in
time, the hook prints nothing and the normal VS Code dialog stays; a click there wins regardless."""

from __future__ import annotations

import itertools
import os
import random
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger("fridayd.permissions")

# A spoken answer counts only if the whole utterance is the answer ("yes", "yeah allow it", "no don't",
# "always allow"): "OK so what's SGLang doing?" or "No, I meant AWS" are questions, not answers, and "why does it
# always fail" must never grant always-allow (13 Nov review).
_YES = r"(?:yes|yeah|yep|yup|sure|ok(?:ay)?|allow(?: it)?|approve(?:d| it)?|go ahead|do it|run it|fine|go for it|let it|" \
       r"sounds good|alright|yes please|allow that)"
_NO = r"(?:no|nope|nah|deny(?: it)?|don'?t|do not|stop|reject(?: it)?|cancel|block(?: it)?|never|no don'?t)"
_FILL = r"(?:\s*(?:,|\.|!|please|friday|it|that|thanks|thank you)\s*)*"
ALLOW = re.compile(rf"^{_YES}(?:\s*,?\s*{_YES})*{_FILL}$", re.I)
DENY = re.compile(rf"^{_NO}(?:\s*,?\s*{_NO})*{_FILL}$", re.I)
ALWAYS = re.compile(rf"^(?:{_YES}\s*,?\s*)?(?:always(?: allow(?: it| that)?)?|allow (?:it |that )?always|"
                    rf"don'?t ask (?:me )?again){_FILL}$", re.I)


@dataclass
class Request:
    id: int
    who: str                 # spoken name of the session ("The AWS master", "dev2 on AWS")
    tool: str
    summary: str             # what is said
    detail: str              # what is shown (the full command / file)
    created: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event)
    session_id: str = ""
    decision: Optional[str] = None     # allow | deny
    always: bool = False
    spoken: bool = False               # Friday asked him out loud (so his spoken answer is expected)


def describe(tool: str, tool_input: dict) -> tuple[str, str]:
    """(what to say, what to show) for a tool call."""
    ti = tool_input or {}
    if tool == "Bash":
        cmd = str(ti.get("command") or "")
        desc = str(ti.get("description") or "").rstrip(".")
        said = f"run a command: {desc[0].lower() + desc[1:]}" if desc else f"run {cmd.split()[0] if cmd else 'a command'}"
        return said, cmd
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = str(ti.get("file_path") or ti.get("notebook_path") or "")
        verb = "write" if tool == "Write" else "edit"
        return f"{verb} {Path(path).name or 'a file'}", path
    if tool == "WebFetch":
        url = str(ti.get("url") or "")
        return f"fetch a page from {urlparse(url).netloc or 'the web'}", url
    if tool == "WebSearch":
        return f"search the web for {str(ti.get('query') or '')[:60]}", str(ti.get("query") or "")
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"use {parts[-1].replace('_', ' ')} from {parts[1] if len(parts) > 2 else 'a connector'}", tool
    return f"use {tool}", str(ti)[:300]


# Tools that reach the PermissionRequest hook but aren't allow/deny: a question for him with options.
NOT_PERMISSIONS = {"AskUserQuestion"}


def _moved_on(path: str, since: int) -> bool:
    """The session wrote a real message (user or assistant: a tool result, the next reply) after ``since``
    bytes. Other records (a worker's queued message, metadata) don't mean the prompt was answered."""
    if not path:
        return False
    try:
        size = os.path.getsize(path)
        if size <= since:
            return False
        with open(path, "rb") as fh:
            fh.seek(since)
            new = fh.read(min(size - since, 4 << 20))
    except OSError:
        return False
    return b'"type":"user"' in new or b'"type":"assistant"' in new


class Permissions:
    def __init__(self, say: Callable[..., None], supervised: Callable[[dict], Optional[str]],
                 at_desk: Callable[[], bool], wait_s: float = 110):
        self.say, self.supervised, self.at_desk, self.wait_s = say, supervised, at_desk, wait_s
        self._ids = itertools.count(random.randint(100000, 899999))   # never reused across restarts (old phone buttons)
        self._lock = threading.Lock()
        self.pending: list[Request] = []
        self.notify: Optional[Callable[[Request], None]] = None   # phone / watch, when he's away

    def ask(self, payload: dict, gone: Optional[Callable[[], bool]] = None) -> dict:
        """From the hook. Blocks until Aman answers here, answers in Claude Code, or ``wait_s`` passes.
        ``gone()`` says the hook's connection closed (Claude Code ended the hook)."""
        tool = str(payload.get("tool_name") or "?")
        if tool in NOT_PERMISSIONS:
            return {"decision": None}           # a question for him, not an allow/deny: Claude Code's own UI
        who = self.supervised(payload)
        if not who:
            return {"decision": None}           # not a session Friday looks after: the normal dialog
        said, shown = describe(str(payload.get("tool_name") or "?"), payload.get("tool_input") or {})
        req = Request(next(self._ids), who, str(payload.get("tool_name") or "?"), said, shown,
                      session_id=str(payload.get("session_id") or ""))
        with self._lock:
            self.pending.append(req)
        here = self.at_desk()
        if here:
            req.spoken = True
            self.say(f"{who} wants to {said}. Allow?", "high", f"permission:{req.id}",
                     lambda: req in self.pending)   # not spoken any more once it's been answered or expired
        if self.notify and not here:
            try:
                self.notify(req)
            except Exception:
                log.exception("phone notification failed")
        log.info("permission %d: %s wants to %s (%s)", req.id, who, said, "asking" if here else "away")
        transcript = str(payload.get("transcript_path") or "")
        try:
            seen = os.path.getsize(transcript) if transcript else 0
        except OSError:
            seen = 0
        deadline = time.time() + self.wait_s
        while not req.done.wait(0.5):
            why = None
            if time.time() > deadline:
                why = "no answer"
            elif gone and gone():
                why = "Claude Code ended the hook"
            elif _moved_on(transcript, seen):
                why = "answered in Claude Code"   # the session moved on: the prompt was settled there
            if why:
                log.info("permission %d: %s; clearing it", req.id, why)
                break
        with self._lock:
            if req in self.pending:
                self.pending.remove(req)
        if req.decision is None:
            log.info("permission %d: no answer; the dialog stays", req.id)
            return {"decision": None}
        return {"decision": req.decision, "always": req.always}

    def pending_for(self, session_id: str) -> bool:
        with self._lock:
            return any(r.session_id == session_id for r in self.pending)

    def asked(self) -> bool:
        """A question she asked him out loud is still waiting for his answer."""
        with self._lock:
            return any(r.spoken for r in self.pending)

    def answer(self, decision: str, rid: Optional[int] = None, always: bool = False) -> Optional[Request]:
        with self._lock:
            req = next((r for r in self.pending if rid is None or r.id == rid), None)
            if req is None or req.done.is_set():
                return None
            self.pending.remove(req)
        req.decision, req.always = decision, always and decision == "allow"
        req.done.set()
        log.info("permission %d: %s%s", req.id, decision, " (always)" if req.always else "")
        return req

    def from_speech(self, text: str) -> Optional[str]:
        """If a permission is waiting and this is an answer to it, settle it and return what to say back."""
        with self._lock:
            if not any(r.spoken for r in self.pending):   # only after she asked him out loud
                return None
        t = re.sub(r"^(?:hey\s+)?friday[\s,.!]*", "", text.strip().lower()).strip(" .!?")
        if len(t.split()) > 6:                            # an answer is a few words; this is a question
            return None
        with self._lock:   # the question she asked last: what "yes" is an answer to
            asked = max((r for r in self.pending if r.spoken), key=lambda r: r.created, default=None)
        if asked is None:
            return None
        if ALWAYS.match(t) or ALLOW.match(t):
            always = bool(ALWAYS.match(t))
            req = self.answer("allow", asked.id, always=always)
            return None if req is None else ("Allowed, and it won't ask for that again." if req.always else "Allowed.")
        if DENY.match(t):
            req = self.answer("deny", asked.id)
            return None if req is None else "Okay, told it no."
        return None

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"id": r.id, "who": r.who, "summary": r.summary, "detail": r.detail, "age_s": int(time.time() - r.created),
                     "created": r.created, "tool": r.tool}
                    for r in self.pending]
