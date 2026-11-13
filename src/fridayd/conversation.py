"""Push-to-talk turns: Aman's words → Friday (Hermes API server) → the voice.

One session ("friday-voice", pinned with X-Hermes-Session-Id) keeps context across turns, so
"and SGLang?" works after a question about AWS. Chat Completions streams the reply token by token
(the Responses API only delivers it at the end), and it is fed straight into one streaming
utterance: the first sentence is spoken as soon as it exists.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from friday_core.paths import STATE_DIR
from typing import Callable, Optional

from .activity import Activity
from .narration import Narrator


class _Collect:
    """Stands in for a speech stream when the answer goes to the phone instead of the speaker."""
    def feed(self, text: str) -> None: pass
    def close(self) -> None: pass

log = logging.getLogger("fridayd.conversation")

VOICE_LOG = STATE_DIR / "voice.jsonl"


class Conversation:
    def __init__(self, speaker, url: str, conversation: str = "friday-voice",
                 ack_sound: str = "/System/Library/Sounds/Tink.aiff", ack_volume: float = 0.25,
                 activity: Optional[Activity] = None, acknowledge: str = "voice", narrate: bool = True,
                 context: Optional[Callable[[], str]] = None, router=None):
        self.speaker = speaker
        self.activity = activity or Activity()
        self.url = url.rstrip("/")
        self.conversation = conversation
        self.ack_sound, self.ack_volume = ack_sound, ack_volume
        self.acknowledge = acknowledge   # "voice": say "Let me look that up." | "tone": the tick | "off"
        self.narrate = narrate           # say slow steps out loud ("Reading your tab.")
        self.context = context           # a line added to each turn (Aman's focus tab in Brave)
        self.router = router             # which thread (Hermes session) each turn goes to; None = one session
        self.summarize: Optional[Callable[[str, str], Optional[str]]] = None   # her thinking -> one spoken line
        self.mic_open: Optional[Callable[[], bool]] = None   # his mic is open (FridayEars): narration waits
        self._turn = 0
        self._lock = threading.Lock()
        self._live: set[int] = set()        # turns in flight
        self._cancelled: set[int] = set()   # turns told to stop (his new question, or Stop)

    @property
    def busy(self) -> bool:
        return bool(self._live)

    def cancel(self) -> None:
        """Stop: every turn in flight stops (its Hermes stream is closed, which interrupts the agent)."""
        with self._lock:
            self._cancelled |= self._live

    def ask(self, text: str, interrupt: bool = True, ack: bool = True,
            reply_to: Optional[Callable[[str], None]] = None, kind: str = "voice",
            title: str = "You asked", shown: Optional[str] = None, hint: Optional[dict] = None) -> bool:
        """A turn in the voice conversation. Aman's push-to-talk turns interrupt Friday and get the
        tick; Friday's own proactive turns (presence notes) do neither. ``shown`` is what the activity
        panel displays as the request (a presence note's wording is internal)."""
        text = text.strip()
        if not text:
            return False
        with self._lock:
            self._turn += 1
            turn = self._turn
            if interrupt:                    # his question replaces what she was doing; her own turns
                self._cancelled |= self._live   # (greetings, offers, phone answers) never cancel his
            self._live.add(turn)
        if interrupt:
            self.speaker.hush()  # talking over Friday interrupts her (queued alerts stay queued)
        if ack and self.acknowledge == "tone" and self.ack_sound and os.path.exists(self.ack_sound):
            subprocess.Popen(["afplay", "-v", str(self.ack_volume), self.ack_sound])
        record = self.activity.begin(kind, text if shown is None else shown, title)
        live = ack and reply_to is None   # Aman's push-to-talk turns: acknowledge and narrate
        threading.Thread(target=self._run, args=(turn, text, reply_to, record, live, kind, hint), name="ask",
                         daemon=True).start()
        return True

    def _current(self, turn: int) -> bool:
        return turn not in self._cancelled

    def _run(self, turn: int, *args, **kwargs) -> None:
        try:
            self._run_turn(turn, *args, **kwargs)
        finally:                            # whatever happens, this turn is no longer in flight
            with self._lock:
                self._live.discard(turn)
                self._cancelled.discard(turn)

    def _run_turn(self, turn: int, text: str, reply_to: Optional[Callable[[str], None]] = None, record=None,
             live: bool = False, origin: str = "voice", hint: Optional[dict] = None) -> None:
        started = time.time()
        reply, error, first = [], None, None
        record = record or self.activity.begin("voice", text, "You asked")
        stream = self.speaker.open_stream(source=record.source) if reply_to is None else _Collect()
        narrator = None
        if live and (self.acknowledge == "voice" or self.narrate):
            narrator = Narrator(stream.feed, lambda: record.reply,
                                lambda cid: self.activity.step_label(record, cid),
                                request=text, summarize=self.summarize, hold=self.mic_open)
            if self.acknowledge == "voice":
                narrator.acknowledge(text)
            if self.narrate:
                narrator.start()
        route = None
        if self.router is not None:
            try:
                route = self.router.route(text, origin=origin, hint=hint)
            except Exception:
                log.exception("routing failed; using the general thread")
        session = route.branch.session if route else self.conversation
        if route:
            self.activity.set_branch(record, route.branch.title)
            if route.announce and reply_to is None and (live or route.action == "fanout"):
                stream.feed(route.announce + " ")     # "Back in the AWS thread." right after the acknowledgment
        try:
            lines = []
            if route:
                lines.append(self.router.branches.context(route.branch))
            try:
                focus = self.context() if self.context else ""
            except Exception:
                focus = ""
            if focus:
                lines.append(focus)
            if route and route.fanout:
                lines.append(self._fanout(route, text, record, turn))
            content = "\n".join(lines + [text]) if lines else text
            for kind, data in self._stream(content, session):
                if not self._current(turn):
                    return
                if kind == "tool":
                    call_id = str(data.get("toolCallId") or "")
                    if data.get("status") == "running":
                        self.activity.tool_start(record, call_id, str(data.get("tool") or "?"), data.get("label"))
                        if narrator:
                            narrator.tool_started(call_id, str(data.get("tool") or "?"))
                    else:
                        self.activity.tool_done(record, call_id, data.get("status") != "failed")
                        if narrator:
                            narrator.tool_finished(call_id)
                    continue
                if kind == "ping":          # Hermes keepalive: only here so a cancelled turn notices promptly
                    continue
                if kind == "reasoning":     # her thinking: not said or shown as is; the narrator condenses it
                    if narrator:
                        narrator.reasoning_arrived(data)
                    continue
                first = first or time.time() - started
                reply.append(data)
                self.activity.text(record, data)
                if narrator:
                    narrator.text_arrived()
                stream.feed(data)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            error = str(e)
            log.warning("ask failed: %s", e)
            if self._current(turn):
                stream.feed("I can't reach the Friday gateway right now, so I couldn't answer that.")
        finally:
            if narrator:
                narrator.stop()
            stream.close()
            cancelled = not self._current(turn)
            self.activity.finish(record, error=error, cancelled=cancelled)
            if route and cancelled and not reply and self.router is not None:
                self.router.cancelled(route)   # nothing came of it: give the thread back
            if route and not error and self._current(turn):
                first_turn = route.branch.turns == 0 and route.branch.kind not in ("general",)
                self.router.after_turn(route, text, "".join(reply).strip())
                if first_turn:   # after Hermes's own auto-title, which runs after the first exchange and would win
                    for delay in (20, 60):
                        t = threading.Timer(delay, self._title_session, args=(session, route.branch.title))
                        t.daemon = True
                        t.start()
            if reply_to is not None and reply and self._current(turn):
                try:
                    reply_to("".join(reply).strip())
                except Exception:
                    log.exception("couldn't deliver the reply")
            entry = {"ts": started, "asked": text, "reply": "".join(reply).strip(), "error": error,
                     "first_text_s": round(first, 2) if first else None,
                     "seconds": round(time.time() - started, 1), "cancelled": not self._current(turn)}
            VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(VOICE_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.info("voice turn %.1fs (first text %.1fs): %r -> %d chars", entry["seconds"], first or 0,
                     text[:80], len(entry["reply"]))

    def _fanout(self, route, text: str, record, turn: int) -> str:
        """Ask each thread the question in its own context, then answer here with what they said.
        Starts are staggered: the next thread starts once the previous one is streaming, so they read
        the shared cache instead of all writing it."""
        answers: dict[str, list[str]] = {b.id: [] for b in route.fanout}
        threads = []
        for i, b in enumerate(route.fanout):
            started = threading.Event()
            call_id = f"fanout-{i}"
            self.activity.tool_start(record, call_id, "fanout_ask", b.title)

            def ask(b=b, started=started, call_id=call_id):
                try:
                    content = self.router.branches.context(b) + "\n" + text
                    for kind, data in self._stream(content, b.session):
                        started.set()
                        if kind == "text":
                            answers[b.id].append(data)
                    self.router.branches.use(b.id)
                except Exception as e:
                    log.warning("fan-out to %s failed: %s", b.title, e)
                finally:
                    started.set()
                    self.activity.tool_done(record, call_id, bool(answers[b.id]))
            th = threading.Thread(target=ask, name=f"fanout-{b.id}", daemon=True)
            th.start()
            threads.append(th)
            started.wait(10)
        for th in threads:
            th.join(240)
        if not self._current(turn):
            return ""
        said = "\n".join(f"- {b.title} thread: {''.join(answers[b.id]).strip()[:1500] or '(no answer)'}"
                         for b in route.fanout)
        return f"[You just asked your other threads this question; their answers:\n{said}]"

    def _title_session(self, session: str, title: str) -> None:
        """Name a new thread's Hermes session, so it reads well in session lists and session_search."""
        try:
            req = urllib.request.Request(f"{self.url}/api/sessions/{urllib.parse.quote(session)}",
                                         data=json.dumps({"title": title}).encode(), method="PATCH", headers={
                                             "Authorization": f"Bearer {os.environ.get('API_SERVER_KEY', '')}",
                                             "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:   # cosmetic
            log.debug("couldn't title %s: %s", session, e)

    def _stream(self, content: str, session: Optional[str] = None):
        body = json.dumps({"model": "hermes-agent", "stream": True,
                           "messages": [{"role": "user", "content": content}]}).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", data=body, method="POST", headers={
            "Authorization": f"Bearer {os.environ.get('API_SERVER_KEY', '')}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": session or self.conversation})
        with urllib.request.urlopen(req, timeout=600) as resp:
            event_type = ""          # SSE "event:" line; Hermes tags tool progress as hermes.tool.progress
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if line.startswith(":"):       # a keepalive comment: let the caller check for cancellation
                    yield "ping", None
                    continue
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[5:])
                except ValueError:
                    continue
                if event_type == "hermes.tool.progress":
                    yield "tool", event
                    continue
                if event_type:
                    continue
                choices = event.get("choices") or []
                delta = choices[0].get("delta", {}) if choices else {}
                if delta.get("reasoning_content"):
                    yield "reasoning", delta["reasoning_content"]
                if delta.get("content"):
                    yield "text", delta["content"]
