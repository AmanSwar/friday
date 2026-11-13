"""What Friday is doing right now. FridayEars shows the current step in the island ("Searching the web
for …", "Checking master_aws", "Speaking") and, when Aman clicks it, the whole turn in a panel: what he
said, each step with its time, and the answer.

Steps come from Hermes's ``hermes.tool.progress`` stream events. Friday's own tools also report a
precise label (``label()``, from the plugin), since their arguments say more than Hermes's preview."""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

# Hermes's built-in tools: a label from the tool name and its preview (its main argument).
BUILTIN = {
    "web_search": "Searching the web for “{p}”",
    "web_extract": "Reading {p}",
    "browser_exec": "Browsing the web",
    "terminal": "Running a command",
    "process": "Checking a running command",
    "execute_code": "Running some code",
    "read_file": "Reading {p}",
    "write_file": "Writing {p}",
    "patch": "Editing {p}",
    "search_files": "Searching files for “{p}”",
    "memory": "Checking my memory",
    "session_search": "Searching past conversations",
    "skill_view": "Reading a skill",
    "skills_list": "Looking through skills",
    "skill_manage": "Updating a skill",
    "cronjob": "Setting up a scheduled job",
    "cronjob_manage": "Setting up a scheduled job",
    "delegate_task": "Handing part of it to a helper",
    "vision_analyze": "Looking at an image",
    "computer_use": "Using the screen",
    "todo": "Planning the steps",
    "clarify": "Asking you something",
    "tool_search": "Getting the right tools",
    "fanout_ask": "Asking the {p} thread",
    "tool_describe": "Getting the right tools",
}
QUIET_TOOLS = {"tool_search", "tool_describe"}   # bookkeeping: shown only if nothing else is happening
SPEECH = "_speech"   # a stretch of Friday talking, recorded as a step so it sits in order with the tools


def _short(preview: str, limit: int = 46) -> str:
    p = re.sub(r"\s+", " ", preview or "").strip()
    if p.startswith(("http://", "https://")):
        host = urlparse(p).netloc
        p = host[4:] if host.startswith("www.") else host or p
    return p if len(p) <= limit else p[: limit - 1].rstrip() + "…"


def describe(tool: str, preview: Optional[str]) -> str:
    pattern = BUILTIN.get(tool)
    if pattern and "{p}" in pattern:
        return pattern.format(p=_short(preview)) if preview else re.sub(r" (for )?“?\{p\}”?", "", pattern)
    if pattern:
        return pattern
    return "Using " + tool.replace("_", " ")


@dataclass
class Step:
    call_id: str
    tool: str
    label: str
    started: float = field(default_factory=time.time)
    ended: Optional[float] = None
    ok: bool = True
    precise: bool = False        # label came from Friday's plugin (knows the arguments)


@dataclass
class Turn:
    id: int
    kind: str                    # voice | phone | presence
    title: str                   # "You asked", "Morning briefing", "From your phone"
    prompt: str
    started: float = field(default_factory=time.time)
    steps: list[Step] = field(default_factory=list)
    reply: str = ""
    ended: Optional[float] = None
    error: Optional[str] = None
    cancelled: bool = False
    branch: str = ""             # which thread (branch) this turn went to
    text_at: float = 0.0         # when the last piece of reply text arrived

    @property
    def spoke(self) -> bool:
        return any(s.tool == SPEECH for s in self.steps)

    @property
    def source(self) -> str:     # the speaker's source tag for this turn's answer
        return f"voice:{self.id}"


class Activity:
    def __init__(self, keep: int = 50):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.turns: deque[Turn] = deque(maxlen=keep)
        self._pending: dict[str, tuple[str, float]] = {}   # tool -> precise label that arrived before its step

    # --- recording (fridayd's conversation thread, the plugin via HTTP) -----------------------------

    def begin(self, kind: str, prompt: str, title: str) -> Turn:
        turn = Turn(next(self._ids), kind, title, prompt)
        with self._lock:
            self.turns.append(turn)
        return turn

    def tool_start(self, turn: Turn, call_id: str, tool: str, preview: Optional[str]) -> None:
        with self._lock:
            pending = self._pending.pop(tool, None)
            if pending and time.time() - pending[1] < 8:
                turn.steps.append(Step(call_id, tool, pending[0], precise=True))
            else:
                turn.steps.append(Step(call_id, tool, describe(tool, preview)))

    def tool_done(self, turn: Turn, call_id: str, ok: bool = True) -> None:
        with self._lock:
            for s in turn.steps:
                if s.call_id == call_id and s.ended is None:
                    s.ended, s.ok = time.time(), ok

    def label(self, tool: str, label: str) -> None:
        """A precise label from Friday's plugin for a step that just started (or is about to)."""
        with self._lock:
            turn = self._active()
            if turn is not None:
                for s in reversed(turn.steps):
                    if s.tool == tool and not s.precise and s.ended is None:
                        s.label, s.precise = label, True
                        return
            self._pending[tool] = (label, time.time())

    def set_branch(self, turn: Turn, title: str) -> None:
        with self._lock:
            turn.branch = title

    def step_label(self, turn: Turn, call_id: str) -> str:
        with self._lock:
            return next((s.label for s in turn.steps if s.call_id == call_id), "")

    def text(self, turn: Turn, delta: str) -> None:
        with self._lock:
            turn.reply += delta
            turn.text_at = time.time()

    def audible(self, source: str, on: bool, line: str = "") -> None:
        """From the speaker: Friday started or stopped making sound for ``source``. A turn that says
        something ("let me check"), goes quiet to work, then answers gets one speech step per stretch."""
        with self._lock:
            turn = next((t for t in reversed(self.turns) if t.source == source), None)
            if turn is None:
                return
            if on:
                words = " ".join(line.split())
                quote = words if len(words) <= 44 else words[:43].rstrip() + "…"
                turn.steps.append(Step(f"speech{len(turn.steps)}", SPEECH, f"Said “{quote}”" if quote else "Spoke"))
            else:
                for st in reversed(turn.steps):
                    if st.tool == SPEECH and st.ended is None:
                        st.ended = time.time()
                        break

    def finish(self, turn: Turn, error: Optional[str] = None, cancelled: bool = False) -> None:
        with self._lock:
            turn.ended, turn.error, turn.cancelled = time.time(), error, cancelled
            for s in turn.steps:
                if s.ended is None and s.tool != SPEECH:   # the voice may still be finishing the answer
                    s.ended, s.ok = turn.ended, not (error or cancelled)

    def stop(self, playing_source: Optional[str] = None) -> None:
        """Aman pressed Stop: nothing more will be done or said for the latest turn. A turn that had already
        finished and been spoken stays as it was (it isn't "stopped")."""
        with self._lock:
            if self.turns:
                t = self.turns[-1]
                now = time.time()
                if t.ended is None or playing_source == t.source:
                    t.cancelled = True
                t.ended = t.ended or now
                for st in t.steps:
                    if st.ended is None:
                        st.ended = now

    def _active(self) -> Optional[Turn]:
        return next((t for t in reversed(self.turns) if t.ended is None), None)

    # --- reading (GET /status for scripts; the /ui stream for FridayEars) ----------------------------

    def _view(self, turn: Turn, now: float, open_source: Optional[str], talking: bool) -> dict:
        mine = open_source == turn.source
        speaking_now = mine and talking
        finished = turn.ended is not None
        steps = [st for st in turn.steps if st.tool not in QUIET_TOOLS] or turn.steps[-1:]
        rows = [{"label": st.label, "status": "running" if st.ended is None else ("done" if st.ok else "failed"),
                 "seconds": round((st.ended or now) - st.started, 1), "started": st.started, "ended": st.ended,
                 "speech": st.tool == SPEECH} for st in steps]
        if turn.kind == "phone" and turn.reply.strip():
            rows.append({"label": "Replying on your phone", "status": "done" if finished else "running",
                         "seconds": None, "started": turn.text_at or now, "ended": turn.ended, "speech": False})
        # The text is done but the answer isn't all said: the voice is rendering it, is between
        # sentences, or waits for an alert to finish. Still Friday's turn, so the island stays up.
        to_speak = (finished and turn.kind != "phone" and bool(turn.reply.strip()) and not turn.cancelled
                    and not turn.error and (mine or (not turn.spoke and now - turn.ended < 25)))
        running = [st for st in turn.steps if st.ended is None and st.tool != SPEECH]
        visible = [st for st in running if st.tool not in QUIET_TOOLS] or running
        if turn.cancelled:
            headline = "Stopped"
        elif turn.error:
            headline = "Couldn't reach the gateway"
        elif speaking_now:
            headline = "Speaking"
        elif to_speak:
            headline = "Getting ready to speak…"
        elif visible:
            headline = visible[-1].label + "…"
        elif not finished and turn.reply.strip() and now - turn.text_at < 2:
            headline = "Writing the answer…"
        elif not finished:
            headline = "Thinking it over…" if turn.steps else "Thinking…"
        else:
            headline = "Done"
        return {
            "id": turn.id, "kind": turn.kind, "title": turn.title, "prompt": turn.prompt, "branch": turn.branch,
            "started": turn.started, "ended": turn.ended, "elapsed": round((turn.ended or now) - turn.started, 1),
            "active": bool(not finished or speaking_now or to_speak), "headline": headline, "steps": rows,
            "reply": turn.reply.strip(), "error": turn.error, "cancelled": turn.cancelled,
        }

    def snapshot(self, open_source: Optional[str], talking: bool, speaking_text: Optional[str],
                 line: Optional[str]) -> dict:
        """``open_source``: the utterance the speaker holds (a streamed answer stays open while Friday works
        between sentences); ``talking``: sound is actually coming out right now."""
        now = time.time()
        with self._lock:
            turn = self.turns[-1] if self.turns else None
            out: dict = {"speaking": None, "turn": None}
            if open_source and talking:
                out["speaking"] = {"source": open_source, "line": line,
                                   "text": None if open_source.startswith("voice:") else speaking_text}
            if turn is not None:
                out["turn"] = self._view(turn, now, open_source, talking)
            return out

    def ui_turn(self, open_source: Optional[str], talking: bool) -> Optional[dict]:
        """The latest turn for FridayEars: timestamps instead of running counters, so the state only
        changes when something happens (the app does the clock)."""
        with self._lock:
            if not self.turns:
                return None
            view = self._view(self.turns[-1], time.time(), open_source, talking)
        view.pop("elapsed", None)
        for row in view["steps"]:
            row.pop("seconds", None)
        return view

    def history(self, limit: int = 50) -> list[dict]:
        """Finished turns, newest first, for the panel's Earlier list."""
        with self._lock:
            done = [t for t in self.turns if t.ended is not None]
        out = []
        for t in reversed(done[-limit:]):
            out.append({"id": f"t{t.id}", "ts": t.started, "kind": "turn", "turn_kind": t.kind, "title": t.title,
                        "thread": t.branch, "text": t.prompt, "reply": t.reply.strip(), "error": t.error,
                        "cancelled": t.cancelled, "seconds": round(t.ended - t.started, 1)})
        return out
