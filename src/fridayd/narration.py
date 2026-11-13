"""Friday sounds alive while she works, and says what she's actually doing.

- An instant acknowledgment when Aman's push-to-talk ends ("Let me look that up.").
- Each step that takes a moment, with its specifics ("Checking the AWS master.", "Searching for vLLM
  tiling.", "Reading notes.md.").
- When nothing new has happened for a while, where things actually are, never a bare "give me a sec":
  1. her own thinking (streamed by Hermes as reasoning), condensed by a quick Haiku call into one spoken
     line in her voice ("Looks like the research report says D7 is a no-go; checking what the master
     did about it.");
  2. the step still running ("The AWS master's still answering.");
  3. what just came back, while she works out the next move ("Got the AWS master's update.");
  4. only after a long wait with nothing to report, a short "still on it".

Everything is spoken through the turn's own speech stream (the speaker plays one utterance at a time and
the answer's stream is already open); none of it goes into the reply text. Rules that keep it bearable:
a step is narrated only if still running after ``STEP_DELAY_S``; nothing within ``QUIET_GAP_S`` of
anything else said; at most ``MAX_LINES`` per turn; never once her answer is flowing or mid-sentence.
Private turns (Signal, his notes, health) are never narrated from her thinking, and private tools never
at all."""

from __future__ import annotations

import random
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .watcher import spoken_name

STEP_DELAY_S = 1.2        # a step must still be running after this long to be worth saying
QUIET_GAP_S = 3.0         # never right after something else was said
MAX_LINES = 7             # narration lines per turn, not counting the acknowledgment
PROGRESS_AFTER_S = 6.0    # nothing new for this long: say where things are
PROGRESS_EVERY_S = 10.0   # and not more often than this
GENERIC_AFTER_S = 14.0    # a bare "still on it" only when there's nothing real to say
GENERIC = ["Still on it.", "Still working on it, hang on.", "Almost there, I think."]

PRIVATE_TOOLS = {"signal_send", "signal_recent", "contact_style", "personal_notes", "health_recent"}
SILENT_TOOLS = {
    "tool_search", "tool_describe", "memory", "todo", "skill_view", "skills_list", "skill_manage",
    "clarify", "friday_alerts", "friday_hush", "brave_tabs", "brave_close", "brave_focus",
    "mac_volume", "mac_open", "mac_quit", "spotify",
} | PRIVATE_TOOLS                 # private: never said out loud

ACKS = {
    "browser": ["Let me look at your tab.", "Okay, looking at it.", "Sure, checking the page."],
    "masters": ["Checking on the masters.", "Let me look at the sessions.", "On it, checking the masters."],
    "slack": ["Checking Slack.", "Let me see what's on Slack."],
    "music": ["Sure, one sec.", "Yep, on it."],
    "notes": ["Got it, writing it down.", "Okay, noting that."],
    "search": ["Let me look that up.", "Searching, one sec.", "On it, searching."],
    "think": ["Hmm, let me think.", "Good question, one sec.", "Okay, let me think about that."],
    "default": ["Okay, got it.", "Yep, on it.", "Sure, give me a sec.", "Alright, one sec."],
}
INTENTS = (   # first match wins: what he asks for (news, a search) beats which project it's about
    ("browser", r"\b(this (page|tab|article|doc|paper)|my tab|on my screen|what am i (looking at|reading)|summari[sz]e this|explain this)\b"),
    ("search", r"\b(search|look up|google|news|trending|twitter|tweets?|latest|weather|price of|what'?s happening)\b"),
    ("slack", r"\bslack\b"),
    ("music", r"\b(play|spotify|music|song|pause|skip|next track)\b"),
    ("notes", r"\b(note|notes|write (it|this) down|obsidian|jot)\b"),
    ("masters", r"\b(master\w*|claude|sessions?|workers?|dev\d|where are we|aws|sglang|trainium|infra)\b"),
    ("think", r"^(why|how (do|does|would|should)|what do you think|should i|explain)\b"),
)

_last_ack: Optional[str] = None


def ack_phrase(text: str) -> str:
    """An acknowledgment that fits the request, never the same one twice in a row."""
    global _last_ack
    t = text.lower()
    intent = next((name for name, pattern in INTENTS if re.search(pattern, t)), "default")
    options = [p for p in ACKS[intent] if p != _last_ack] or ACKS[intent]
    _last_ack = random.choice(options)
    return _last_ack


# --- what a step is, in words -----------------------------------------------------------------------

def _between(label: str, prefix: str) -> str:
    return label[len(prefix):].strip(" “”\"'") if label.startswith(prefix) else ""


def _quoted(label: str) -> str:
    m = re.search(r"[“\"]([^”\"]+)[”\"]", label)
    return m.group(1).strip() if m else ""


def _who(label: str) -> str:
    """'Checking master_aws' / 'Asking master_aws directly' -> 'the AWS master'."""
    session = re.sub(r"\s+directly$", "", _between(label, "Checking") or _between(label, "Asking")
                     or _between(label, "Sending a prompt to"))
    if session.startswith("master_"):
        return f"the {spoken_name(session)} master"
    return session or "the session"


def _file(label: str) -> str:
    rest = _between(label, "Reading") or _between(label, "Writing") or _between(label, "Editing")
    name = Path(rest.rstrip("…")).name if rest else ""
    return name if name and len(name) <= 40 else ""


def step_phrase(tool: str, label: str) -> Optional[str]:
    """What to say for a step that's taking a moment, or None to stay quiet."""
    if tool in SILENT_TOOLS:
        return None
    label = (label or "").strip()
    if tool in ("claude_status", "claude_ask", "claude_send"):
        who = _who(label)
        return {"claude_status": f"Checking {who}.", "claude_ask": f"Asking {who} directly. This takes a bit.",
                "claude_send": f"Sending it to {who}."}[tool]
    if tool == "claude_dispatch":
        folder = _between(label, "Starting a Claude Code session in")
        return f"Starting a Claude Code session in {folder}." if folder else "Starting a Claude Code session."
    if tool == "claude_sessions":
        return "Looking over your sessions."
    if tool == "web_search":
        q = _quoted(label) or re.sub(r"^Searching the web for\s*", "", label).strip(" “”\"'")
        return f"Searching for {q}." if q and len(q) <= 50 and q != label else "Searching the web."
    if tool == "web_extract":
        host = _between(label, "Reading")
        return f"Reading {host}." if host and len(host) <= 40 else "Reading through it."
    if tool == "browser_exec":
        return "Browsing for it."
    if tool == "brave_open":
        q = _quoted(label) if label.startswith("Searching Google") else ""
        if q:
            return f"Searching Google for {q}." if len(q) <= 50 else "Searching Google."
        host = _between(label, "Opening").replace(" in Brave", "").strip()
        if host in ("x.com", "twitter.com"):
            return "Pulling up X."
        return f"Opening {host}." if host else "Opening the page."
    if tool == "brave_read":
        return "Reading your tab." if "your" in label else "Reading the page."
    if tool == "brave_click":
        what = _quoted(label)
        return f"Clicking {what}." if what and len(what) <= 30 else "Clicking through."
    if tool == "slack_catchup":
        return "Going through Slack."
    if tool == "slack_read":
        ch = _between(label, "Reading").replace(" on Slack", "").strip()
        return f"Reading {ch} on Slack." if ch else "Reading Slack."
    if tool == "slack_search":
        q = _quoted(label)
        return f"Searching Slack for {q}." if q else "Searching Slack."
    if tool in ("read_file", "write_file", "patch"):
        name = _file(label)
        verb = {"read_file": "Reading", "write_file": "Writing", "patch": "Editing"}[tool]
        return f"{verb} {name}." if name else ("Going through the files." if tool == "read_file" else "Making the change.")
    if tool == "search_files":
        q = _quoted(label)
        return f"Searching the files for {q}." if q and len(q) <= 40 else "Searching the files."
    if tool in ("terminal", "process"):
        return "Running a command." if tool == "terminal" else "Checking on that command."
    if tool == "execute_code":
        return "Running some code."
    if tool in ("obsidian", "apple_notes"):
        return "Writing it down." if label.startswith(("Writing", "Adding", "Creating")) else "Looking in your notes."
    if tool == "session_search":
        return "Looking through our past conversations."
    if tool == "delegate_task":
        return "Handing part of this to a helper."
    if tool == "fanout_ask":
        thread = _between(label, "Asking the").replace(" thread", "").strip()
        return f"Asking the {thread} thread." if thread else "Asking your other threads."
    if tool == "computer_use":
        return "Working on the screen."
    if tool == "vision_analyze":
        return "Looking at the image."
    if tool in ("cronjob", "cronjob_manage"):
        return "Setting that up."
    if label and not label.startswith("Using ") and len(label) <= 60:   # any other tool with a real label
        return label.rstrip(".…") + "."
    return None


def still_phrase(tool: str, label: str) -> Optional[str]:
    """A step that's been running a while: what she's still waiting on."""
    if tool in SILENT_TOOLS:
        return None
    if tool == "claude_ask":
        who = _who(label)
        return random.choice([f"{who[0].upper() + who[1:]}'s still answering.",
                              f"Still waiting on {who}. It's thinking it through."])
    if tool == "claude_status":
        return f"Still pulling {_who(label)}'s status."
    if tool == "web_search":
        return "Still searching."
    if tool in ("web_extract", "brave_read", "browser_exec"):
        return "Still reading through it."
    if tool in ("terminal", "process", "execute_code"):
        return "That's still running."
    if tool == "delegate_task":
        return "The helper's still on it."
    if tool == "fanout_ask":
        return "Still waiting on the other threads."
    if tool == "slack_catchup":
        return "Still going through Slack."
    return None


def got_phrase(tool: str, label: str) -> Optional[str]:
    """Between steps (she's deciding what's next): what just came back."""
    if tool in SILENT_TOOLS:
        return None
    if tool in ("claude_status", "claude_ask"):
        return f"Got {_who(label)}'s update. Putting it together."
    if tool == "claude_sessions":
        return "Got the sessions. Working out which ones matter."
    if tool == "web_search":
        return "Got some results. Going through them."
    if tool in ("web_extract", "brave_read"):
        return "Read it. Working out the answer."
    if tool in ("terminal", "execute_code"):
        return "Got the output. Making sense of it."
    if tool == "fanout_ask":
        return "Got the other threads' answers. Putting them together."
    if tool in ("slack_catchup", "slack_read", "slack_search"):
        return "Got what's on Slack. Sorting it out."
    return None


class Narrator(threading.Thread):
    """One per push-to-talk turn. ``say`` feeds the turn's speech stream; ``reply_text`` is what Claude has
    said so far (to avoid cutting into one of its sentences); ``step_label`` reads a step's current label
    (Friday's plugin sharpens it a moment after the step starts); ``summarize(request, notes)`` turns her
    raw thinking into one spoken line (or None)."""

    def __init__(self, say: Callable[[str], None], reply_text: Callable[[], str],
                 step_label: Callable[[str], str], request: str = "",
                 summarize: Optional[Callable[[str, str], Optional[str]]] = None,
                 hold: Optional[Callable[[], bool]] = None):
        super().__init__(name="narrator", daemon=True)
        self.say, self.reply_text, self.step_label = say, reply_text, step_label
        self.request, self.summarize = request, summarize
        self.hold = hold                                   # True while his mic is open: say nothing
        self._lock = threading.Lock()
        self._running: dict[str, tuple[str, float]] = {}   # call id -> (tool, started)
        self._finished: list[tuple[str, str]] = []         # (tool, label), in order
        self._narrated: set[str] = set()
        self._said: set[str] = set()                       # never the same line twice in a turn
        self._thoughts = ""                                # her reasoning not yet narrated
        self._private = False
        self._summarizing = False
        self._got_for = -1                                 # index of the finished step already "got"
        self._done = threading.Event()
        self.lines = 0
        self.last_said = time.time()
        self.last_event = time.time()
        self.last_progress = 0.0
        self.text_at = 0.0
        self.tool_at = 0.0

    # --- events from the conversation thread --------------------------------------------------------

    def acknowledge(self, request: str) -> None:
        self._speak(ack_phrase(request), count=False)

    def text_arrived(self) -> None:
        with self._lock:
            self.text_at = self.last_event = self.last_said = time.time()

    def reasoning_arrived(self, text: str) -> None:
        with self._lock:
            self._thoughts = (self._thoughts + text)[-4000:]

    def tool_started(self, call_id: str, tool: str) -> None:
        with self._lock:
            self._running[call_id] = (tool, time.time())
            self.tool_at = self.last_event = time.time()
            if tool in PRIVATE_TOOLS:
                self._private = True

    def tool_finished(self, call_id: str) -> None:
        label = self.step_label(call_id)
        with self._lock:
            tool, _ = self._running.pop(call_id, ("?", 0.0))
            self._finished.append((tool, label))
            self.tool_at = self.last_event = time.time()

    def stop(self) -> None:
        self._done.set()

    # --- the watchdog ------------------------------------------------------------------------------

    def _moment(self) -> tuple[bool, float, float]:
        """(a good moment to talk: Claude isn't mid-answer or mid-sentence, quiet for, idle for)."""
        now = time.time()
        answering = self.text_at > self.tool_at and not self._running   # Claude is talking now
        text = self.reply_text().rstrip()
        clean = not text or text[-1] in ".!?:"
        return (not answering and clean, now - self.last_said, now - self.last_event)

    def run(self) -> None:
        while not self._done.wait(0.25):
            now = time.time()
            with self._lock:
                ok, quiet, idle = self._moment()
                due = [(cid, tool) for cid, (tool, started) in self._running.items()
                       if cid not in self._narrated and now - started >= STEP_DELAY_S]
            if not ok or self.lines >= MAX_LINES or (self.hold is not None and self.hold()):
                continue
            if due and quiet >= QUIET_GAP_S:
                cid, tool = due[0]
                self._narrated.add(cid)
                phrase = step_phrase(tool, self.step_label(cid))
                if phrase:
                    self._speak(phrase)
                continue
            if idle >= PROGRESS_AFTER_S and quiet >= PROGRESS_AFTER_S and now - self.last_progress >= PROGRESS_EVERY_S:
                line = self._progress(idle)
                if line:
                    self.last_progress = now
                    self._speak(line)

    def _progress(self, idle: float) -> Optional[str]:
        with self._lock:
            # 1. her own thinking, condensed (never on a private turn)
            if self.summarize and not self._private and not self._summarizing and len(self._thoughts.strip()) >= 40:
                notes, self._thoughts = self._thoughts[-1500:], ""
                self._summarizing = True
                self.last_progress = time.time()
                threading.Thread(target=self._from_thoughts, args=(notes,), name="narrate-thought", daemon=True).start()
                return None
            running = sorted(self._running.items(), key=lambda kv: kv[1][1])
            finished = list(self._finished)
        # 2. a step still running: what she's waiting on
        if running:
            cid, (tool, _) = running[-1]
            line = still_phrase(tool, self.step_label(cid))
            if line and line not in self._said:
                return line
        # 3. between steps: what just came back
        elif finished and self._got_for < len(finished) - 1:
            self._got_for = len(finished) - 1
            tool, label = finished[-1]
            line = got_phrase(tool, label)
            if line and line not in self._said:
                return line
        # 4. nothing real to say, and it's been a while
        if idle >= GENERIC_AFTER_S:
            return next((g for g in GENERIC if g not in self._said), None)
        return None

    def _from_thoughts(self, notes: str) -> None:
        try:
            line = self.summarize(self.request, notes) if self.summarize else None
        except Exception:
            line = None
        with self._lock:
            self._summarizing = False
            ok, quiet, _ = self._moment()
        if not line or self._done.is_set() or not ok or quiet < QUIET_GAP_S or self.lines >= MAX_LINES:
            return
        if self.hold is not None and self.hold():
            return
        self._speak(line)

    def _speak(self, phrase: str, count: bool = True) -> None:
        phrase = phrase.strip()
        if not phrase:
            return
        with self._lock:
            self.last_said = time.time()
            self._said.add(phrase)
            if count:
                self.lines += 1
        self.say(phrase + " ")    # trailing space: the speaker cuts at the sentence end right away
