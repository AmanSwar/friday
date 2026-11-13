"""Follow Claude Code hook events and turn the ones about master sessions into alerts."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from friday_core.claude import Registry, Session, last_turn, recent_records, render_digest
from friday_core.paths import EVENTS_FILE

log = logging.getLogger("fridayd.watcher")


@dataclass
class Alert:
    session_id: str
    title: str
    kind: str          # finished | needs_permission | question | error | input_needed
    line: str
    urgency: str = "normal"
    reason: str = ""
    ts: float = field(default_factory=time.time)
    delivered: str = ""  # spoken | phone | notification | held

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class SessionState:
    activity: str = "unknown"
    since: float = field(default_factory=time.time)
    last_event: str = ""


def spoken_name(title: str) -> str:
    name = title.split("_", 1)[1] if "_" in title else title
    return {"aws": "AWS", "sglang": "SGLang", "infra": "infra", "mulgpu": "multi-GPU"}.get(name, name)


class Watcher(threading.Thread):
    def __init__(self, registry: Registry, judge, deliver: Callable[[Alert], None],
                 events_file: Path = EVENTS_FILE, debounce_s: float = 20):
        super().__init__(name="watcher", daemon=True)
        self.registry = registry
        self.judge = judge
        self.deliver = deliver
        self.events_file = events_file
        self.debounce_s = debounce_s
        self.states: dict[str, SessionState] = {}
        self._meta: dict[str, tuple[float, Optional[Session]]] = {}
        self._recent: dict[tuple[str, str], float] = {}
        self._pool = threading.Semaphore(4)  # at most 4 judge calls in flight
        self.worker_signals: dict[str, list[str]] = {}   # master title -> failures its workers hit since its last turn
        self._signals_lock = threading.Lock()
        self.on_signal: Optional[Callable[[str, str], None]] = None   # (master title, note) for the project's thread
        self.judging: dict[str, float] = {}   # who Friday is checking right now -> since (FridayEars shows it small)
        self.asking_about = None   # sid -> bool: Friday is already asking him about a permission for that session

    # --- event intake ------------------------------------------------------------------------

    def run(self) -> None:
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.events_file.touch(exist_ok=True)
        fh = open(self.events_file, "r", encoding="utf-8")
        fh.seek(0, 2)  # only new events; history is not replayed on restart
        inode = self.events_file.stat().st_ino
        buf = ""
        while True:
            chunk = fh.read()
            if chunk:
                buf += chunk
                *lines, buf = buf.split("\n")
                for line in lines:
                    self._handle_line(line)
                continue
            time.sleep(0.25)
            try:
                st = self.events_file.stat()
            except FileNotFoundError:
                continue
            if st.st_ino != inode or st.st_size < fh.tell():  # rotated or truncated
                fh.close()
                fh = open(self.events_file, "r", encoding="utf-8")
                inode = st.st_ino

    def _handle_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        try:
            self.handle(event)
        except Exception:
            log.exception("failed to handle event %s", line[:200])

    def session_for(self, event: dict) -> Optional[Session]:
        sid = event.get("session_id") or ""
        cached = self._meta.get(sid)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        path = event.get("transcript_path")
        session = self.registry.by_path(path) if path else None
        self._meta[sid] = (time.time(), session)
        return session

    # --- event → state / alert ---------------------------------------------------------------

    def _set(self, sid: str, activity: str, event_name: str) -> None:
        st = self.states.setdefault(sid, SessionState())
        if st.activity != activity:
            st.activity, st.since = activity, time.time()
        st.last_event = event_name

    def _debounced(self, sid: str, kind: str) -> bool:
        now = time.time()
        key = (sid, kind)
        if now - self._recent.get(key, 0) < self.debounce_s:
            return True
        self._recent[key] = now
        return False

    def handle(self, event: dict) -> None:
        name = event.get("hook_event_name", "")
        sid = event.get("session_id", "")
        if not sid:
            return
        session = self.session_for(event)
        ntype = event.get("notification_type", "")

        if name == "UserPromptSubmit":
            self._set(sid, "working", name)
        elif name == "Stop":
            self._set(sid, "finished its turn", name)
        elif name == "StopFailure":
            self._set(sid, "stopped on an error", name)
        elif name == "Notification" and ntype == "permission_prompt":
            self._set(sid, "waiting for your permission", name)
        elif name == "PreToolUse" and event.get("tool_name") == "AskUserQuestion":
            self._set(sid, "asking you a question", name)
        elif name == "Notification" and ntype == "elicitation_dialog":
            self._set(sid, "waiting for your input", name)

        if name == "Failure":   # a worker's (or master's) tool output shows a failure: a note, not an interruption
            master = event.get("master") or ""
            text = f"{event.get('who', 'a worker')} hit: {event.get('snippet', 'a failure')}"
            with self._signals_lock:
                self.worker_signals.setdefault(master, []).append(text)
                self.worker_signals[master] = self.worker_signals[master][-6:]
            if self.on_signal and not self._debounced(f"{sid}:{event.get('snippet', '')[:40]}", "failure"):
                self.on_signal(master, text)
            return
        if name == "Stall":
            if not self._debounced(sid, "stall"):
                threading.Thread(target=self._judge_stall, args=(event,), daemon=True).start()
            return

        if not (session and session.is_master):
            return
        title = session.title or sid[:8]
        who = spoken_name(title)

        if name == "Notification" and ntype == "permission_prompt":
            message = str(event.get("message") or "")
            if "AskUserQuestion" in message:      # a question: the PreToolUse alert covers it
                return
            if self.asking_about is not None and self.asking_about(sid):
                return                            # Friday is already asking him (the PermissionRequest hook)
            if not self._debounced(sid, "permission"):
                what = (event.get("message") or "").replace("Claude needs your permission to use ", "")
                line = f"{who} needs your permission" + (f" to use {what}." if what else ".")
                self.deliver(Alert(sid, title, "needs_permission", line, "high"))
        elif name == "PreToolUse" and event.get("tool_name") == "AskUserQuestion":
            if not self._debounced(sid, "question"):
                q = (event.get("questions") or ["a question"])[0]
                self.deliver(Alert(sid, title, "question", f"{who} is asking you: {q}", "high"))
        elif name == "Notification" and ntype == "elicitation_dialog":
            if not self._debounced(sid, "input"):
                self.deliver(Alert(sid, title, "input_needed", f"{who} is waiting for your input.", "high"))
        elif name == "StopFailure":
            if not self._debounced(sid, "error"):
                err = event.get("error") or event.get("error_type") or "an error"
                self.deliver(Alert(sid, title, "error", f"{who} stopped on an error: {err}", "high"))
        elif name == "Stop" and not event.get("stop_hook_active"):
            if not self._debounced(sid, "stop"):   # hooks and the transcript tailer can both report one turn
                threading.Thread(target=self._judge_stop, args=(session, sid), daemon=True).start()

    def _judge_stop(self, session: Session, sid: str) -> None:
        key = session.title or sid[:8]
        self.judging[key] = time.time()
        try:
            self._judge_stop_inner(session, sid)
        finally:
            self.judging.pop(key, None)

    def _judge_stop_inner(self, session: Session, sid: str) -> None:
        with self._pool:
            turn = last_turn(recent_records(Path(session.path), human_turns=1, max_prompts=2))
            if turn is None:
                return
            with self._signals_lock:
                signals = self.worker_signals.pop(session.title or "", [])
            decision = self.judge.decide(session, turn, signals) if self.judge else None
            if decision is None:
                if turn.origin != "human":
                    return
                decision = {"speak": True, "urgency": "normal", "reason": "fallback",
                            "line": f"{spoken_name(session.title or '')} finished what you asked."}
            log.info("stop %s origin=%s speak=%s reason=%s", session.title, turn.origin,
                     decision.get("speak"), decision.get("reason", "")[:120])
            if decision.get("speak"):
                self.deliver(Alert(sid, session.title or sid[:8], "finished", decision["line"],
                                   decision.get("urgency", "normal"), decision.get("reason", "")))


    def _judge_stall(self, event: dict) -> None:
        """A session went quiet mid-work: ask whether it's stuck or legitimately waiting."""
        key = str(event.get("who") or "a session")
        self.judging[key] = time.time()
        try:
            self._judge_stall_inner(event)
        finally:
            self.judging.pop(key, None)

    def _judge_stall_inner(self, event: dict) -> None:
        with self._pool:
            path = Path(event.get("transcript_path", ""))
            try:
                digest = render_digest(recent_records(path, human_turns=1, max_prompts=3), budget=6000)
            except OSError:
                return
            decision = self.judge.stalled(event.get("who", "a session"), event.get("master", ""), event.get("minutes", 0),
                                          digest) if self.judge else None
            log.info("stall %s (%s min) speak=%s reason=%s", event.get("who"), event.get("minutes"),
                     (decision or {}).get("speak"), ((decision or {}).get("reason") or "")[:120])
            if decision and decision.get("speak"):
                self.deliver(Alert(event.get("session_id", ""), event.get("master") or event.get("who", ""), "stalled",
                                   decision["line"], decision.get("urgency", "normal"), decision.get("reason", "")))


# Failures worth knowing about in tool output (raw transcript bytes, so no JSON parse for most lines).
FAILURE = re.compile(rb"Traceback \(most recent call last\)|CUDA out of memory|OutOfMemoryError|NO-RESULT|Segmentation fault|"
                     rb"No space left on device|InstanceTerminated|[Pp]reempted|ConnectionRefusedError|\bKilled\b")
WORKER = re.compile(r"^(dev|research|worker)", re.I)


class TranscriptTail(threading.Thread):
    """Signals from Claude Code transcripts, without polling a model. Follows each master (active in the
    last 3 days) and its workers (dev*/research_* sessions launched from the master's folder, active in
    the last day) from the end of their transcripts:
    - masters: a final message (stop_reason end_turn) becomes Stop, an AskUserQuestion call the question
      event, a prompt Aman typed marks it working (permission prompts still need the hooks);
    - masters and workers: failures in tool output (Traceback, out of memory, NO-RESULT, Killed, ...)
      become Failure notes, and a session that writes nothing for ``stall_s`` while it's in the middle of
      a tool call or waiting on the model becomes one Stall event (judged before anything is said)."""

    def __init__(self, registry: Registry, handle: Callable[[dict], None], poll_s: float = 2.0, quiet_s: float = 2.5,
                 stall_s: float = 1200):
        super().__init__(name="transcripts", daemon=True)
        self.registry, self.handle = registry, handle
        self.poll_s, self.quiet_s, self.stall_s = poll_s, quiet_s, stall_s
        self.offsets: dict[str, int] = {}
        self.pending: dict[str, tuple[float, str, str]] = {}   # sid -> (when, path, message id) awaiting quiet
        self.phase: dict[str, list] = {}       # path -> [phase ("tool" | "model" | "idle"), stall reported, since]
        self._watched: list[tuple[Session, str, bool]] = []
        self._watched_at = 0.0

    def watched(self) -> list[tuple[Session, str, bool]]:
        """(session, its master's title, is master)."""
        if time.time() - self._watched_at > 60:
            now = time.time()
            masters = [m for m in self.registry.masters() if now - m.mtime < 3 * 86400]
            folders = {m.project_dir: m.title for m in masters if m.project_dir}
            workers = []
            try:
                for s in self.registry.sessions():
                    if (not s.is_master and s.project_dir in folders and now - s.mtime < 86400
                            and WORKER.match(s.title or "")):
                        workers.append((s, folders[s.project_dir], False))
            except Exception:
                log.exception("listing workers failed")
            self._watched = [(m, m.title or m.session_id[:8], True) for m in masters] + workers
            self._watched_at = now
        return self._watched

    def masters(self) -> list[Session]:
        return [s for s, _, is_master in self.watched() if is_master]

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("transcript tail failed")
            time.sleep(self.poll_s)

    def tick(self) -> None:
        now = time.time()
        for m, master, is_master in self.watched():
            try:
                st = Path(m.path).stat()
            except FileNotFoundError:
                continue
            size = st.st_size
            start = self.offsets.get(m.path)
            if start is None or size < start:      # first sight (don't replay history) or truncated
                self.offsets[m.path] = size
                continue
            if size > start:
                self.read(m, start, size, master, is_master)
            ph = self.phase.get(m.path)
            if ph and ph[0] in ("tool", "model") and not ph[1] and now - st.st_mtime > self.stall_s:
                ph[1] = True                        # once per quiet stretch; any new write re-arms it
                self.handle({"hook_event_name": "Stall", "session_id": m.session_id, "transcript_path": m.path,
                             "master": master, "who": m.title or m.session_id[:8], "worker": not is_master,
                             "minutes": int((now - st.st_mtime) // 60), "source": "transcript"})
        for sid, (when, path, _) in list(self.pending.items()):
            if now - when >= self.quiet_s:         # the final message's blocks have all landed
                del self.pending[sid]
                self.handle({"hook_event_name": "Stop", "session_id": sid, "transcript_path": path, "source": "transcript"})

    def read(self, m: Session, start: int, end: int, master: str = "", is_master: bool = True) -> None:
        with open(m.path, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start)
        cut = data.rfind(b"\n") + 1                   # only whole lines; the rest is read next time
        self.offsets[m.path] = start + cut
        ph = self.phase.setdefault(m.path, ["idle", False, time.time()])
        for line in data[:cut].splitlines():
            if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
                continue
            fail = FAILURE.search(line) if b'"tool_result"' in line else None
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("isSidechain"):
                continue
            ph[1] = False                             # it wrote something: re-arm stall detection
            sid = rec.get("sessionId") or m.session_id
            base = {"session_id": sid, "transcript_path": m.path, "source": "transcript"}
            msg = rec.get("message") or {}
            blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
            if rec["type"] == "assistant":
                new = "idle" if msg.get("stop_reason") == "end_turn" else (
                    "tool" if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks) else ph[0])
            else:
                new = "model"
            if (new == "idle") != (ph[0] == "idle"):
                ph[2] = time.time()                   # went busy, or went idle: the panel shows how long
            ph[0] = new
            if fail:
                self.handle({**base, "hook_event_name": "Failure", "master": master, "who": m.title or sid[:8],
                             "snippet": fail.group(0).decode("utf-8", "replace")})
            if not is_master:
                continue
            if rec["type"] == "user":
                content = (rec.get("message") or {}).get("content")
                typed = isinstance(content, str) or (isinstance(content, list) and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content))
                if typed and rec.get("turnOrigin", "human") == "human":
                    self.handle({**base, "hook_event_name": "UserPromptSubmit"})
                continue
            for b in blocks:
                if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                    qs = [q.get("question", "") for q in (b.get("input") or {}).get("questions") or [] if isinstance(q, dict)]
                    self.handle({**base, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "questions": qs})
            if msg.get("stop_reason") == "end_turn":
                self.pending[sid] = (time.time(), m.path, msg.get("id", ""))
            elif sid in self.pending and msg.get("id") != self.pending[sid][2]:
                del self.pending[sid]                 # a new message started: that turn didn't end after all
