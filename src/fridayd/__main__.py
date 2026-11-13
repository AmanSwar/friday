"""fridayd: Friday's always-on half. Owns the voice, watches Claude Code, knows if Aman is at the desk.

    POST /speak   {"text", "priority"?, "source"?}   queue speech (replies from the Hermes plugin)
    POST /ask     {"text"}                           a push-to-talk turn: ask Friday, speak the answer
    POST /stop                                       silence now, drop the queue, cancel a pending ask
    POST /quit                                       exit (FridayEars calls this when it quits)
    POST /presence {"face": true}                    FridayEars' camera saw a face (counts as at the desk)
    POST /branch {"id"}                              switch the current thread (from the panel)
    POST /activity {"tool", "label"}                 Friday's plugin names the step it's running (for the island)
    POST /browser {"action", "params"}               run a command in Aman's Brave (extensions/brave)
    POST /quiet {"on"}                               quiet mode: captions in the island instead of speech
    POST /mic {"open"}                               FridayEars is listening: keep music and videos down
    POST /held/play                                  say the alerts held while he typed or was away, now
    POST /open-session {"session_id"}                show a Claude Code session in VS Code (the panel's masters)
    GET  /ui, /ui/stream                             FridayEars' state: latest, or pushed as it changes (fridayd/ui.py)
    GET  /status                                     presence, voice, per-session activity, recent alerts (scripts)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from friday_core.claude import Registry
from friday_core.paths import ALERTS_FILE, DAEMON_URL, FRIDAY_HOME, STATE_DIR

from . import presence
from .conversation import Conversation
from .activity import Activity
from .branches import Branches
from .browser import BraveBridge, BridgeError
from .permissions import Permissions
from .phone import Phone, latest_health
from .router import Router
from .watcher import WORKER, spoken_name
from .ducking import Ducker
from .inbox import SignalWatch, SlackWatch
from .llm import Judge, slack_headsup
from .speech import FallbackEngine, KokoroEngine, MlxEngine, PiperEngine, RemoteEngine, Speaker
from .watcher import Alert, TranscriptTail, Watcher
from .ui import UIHub

UI_PREFS = STATE_DIR / "ui.json"      # quiet mode survives restarts

# Her thinking, condensed into one line she says while she works (narration.py, step 1 of a progress line).
NARRATE_SYSTEM = """You are Friday, Aman's voice assistant, partway through something he asked. Out loud, in one \
short line, tell him where you are: what you're doing, or what you just found. You get his request and your own \
private working notes.
- One sentence, at most 16 words, first person, casual spoken English. No markdown, quotes, or lists.
- Say the substance ("Looks like dev2's run died on an OOM, so I'm checking what the master did."), never a \
generic "working on it".
- Don't give the final answer and don't repeat his question.
- Never read out private messages, names from them, or personal details.
- If the notes have nothing worth saying out loud, reply exactly SKIP."""
NEEDS_YOU = {"waiting for your permission", "asking you a question", "waiting for your input"}

log = logging.getLogger("fridayd")

DEFAULTS = {
    # Friday's voice: designed once with Qwen3-TTS VoiceDesign (the sentence Aman picked), then cloned on
    # every line by the Base model, so all sentences are the same voice. VoiceDesign alone re-samples
    # the voice per sentence (5-6 different voices in one reply).
    "speech": {"engine": "remote", "remote_url": "http://127.0.0.1:8091", "model": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16",
               "ref_audio": str(FRIDAY_HOME / "voices" / "friday-ref.wav"),
               "ref_text": ("Hey, so, quick update. The AWS run finished, and it passed, so that's nice. SGLang's still "
                                "going through the sweep. Nothing you need to do right now, though. I'll let you know if that changes."),
               "seed": 7, "kokoro_voice": "af_heart", "speed": 1.15, "int8": True,
               "piper_voice": "en_GB-alan-medium", "length_scale": 0.85},
    # focus_keys_per_min: typing at least this fast holds routine alerts until he pauses (urgent ones still speak).
    "presence": {"away_after_s": 300, "mic_hold_s": 90, "focus_keys_per_min": 80},
    # push-to-talk turns. acknowledge: "voice" says "Let me look that up." the moment you let go ("tone" = a tick,
    # "off"); narrate: says slow steps out loud ("Reading your tab.") so a long turn doesn't sound hung.
    "voice": {"hermes_url": "http://127.0.0.1:8652", "conversation": "friday-voice", "acknowledge": "voice",
              "narrate": True},
    "proactive": {"welcome_back_after_s": 900, "quiet_return_s": 7200, "morning_after_s": 4 * 3600,
                  "morning_from_hour": 5,
                  # Offer help when he's working hard to understand a page in Brave (the extension notices).
                  "browser_help": True, "browser_help_cooldown_s": 1200},
    # stall_s: a master or worker that writes nothing this long mid-work gets checked (stuck vs waiting).
    "alerts": {"model": "claude-sonnet-5-5", "effort": "low", "debounce_s": 20, "stall_s": 1200},
    # iPhone + Apple Watch through the ntfy app (scripts/phone-setup.py fills these in). command: an older,
    # optional fallback (text appended as the last argument).
    "phone": {"enabled": True, "command": "", "ntfy_server": "https://ntfy.sh", "ntfy_topic": "", "reply_topic": "", "ntfy_token": ""},
    # Signal DMs. "quiet": never interrupts and never says what anyone wrote; a greeting that happens anyway
    # may add who messaged. "off": never mentions Signal. "full": speaks each message as it arrives.
    "signal": {"announce": "quiet"},
    # Threads: each topic gets its own Hermes session (fridayd/branches.py, router.py). The router's
    # classifier only runs for turns the rules can't place.
    "branches": {"enabled": True, "router_model": "claude-haiku-5-5", "follow_up_s": 120,
                 "classifier_timeout_s": 4.0, "archive_after_days": 7},
    # Lower Spotify / Apple Music / media in Brave tabs while Friday talks; level is the volume they drop to.
    "ducking": {"enabled": True, "level": 10, "restore_after_s": 1.2, "apps": ["spotify", "music", "brave"]},
}


def load_config() -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    path = FRIDAY_HOME / "friday.toml"
    if path.exists():
        for section, values in tomllib.loads(path.read_text()).items():
            cfg.setdefault(section, {}).update(values)
    return cfg


def load_env() -> None:
    """Secrets live in ~/.friday/.env (shared with Hermes); launchd gives us no shell environment."""
    path = FRIDAY_HOME / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Friday:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        sp = cfg["speech"]
        def mac_voice() -> MlxEngine:  # Qwen3-TTS on this Mac's GPU, cloning Friday's reference voice
            return MlxEngine(sp["model"], seed=sp.get("seed"), voice=sp.get("voice"), instruct=sp.get("instruct"),
                             ref_audio=os.path.expanduser(sp["ref_audio"]) if sp.get("ref_audio") else None,
                             ref_text=sp.get("ref_text"))
        if sp["engine"] == "remote":   # the laptop's GPU, the Mac voice as fallback
            engine = FallbackEngine(RemoteEngine(sp["remote_url"]), mac_voice)
        elif sp["engine"] == "mlx":
            engine = mac_voice()
        elif sp["engine"] == "piper":
            engine = PiperEngine(FRIDAY_HOME / "voices" / f"{sp['piper_voice']}.onnx", sp["length_scale"])
        else:
            engine = KokoroEngine(FRIDAY_HOME / "voices" / "kokoro", sp["kokoro_voice"], sp["speed"], sp["int8"])
        self.speaker = Speaker(engine, presence.mic_busy, cfg["presence"]["mic_hold_s"], cfg.get("pronunciations"))
        self.activity = Activity()   # what Friday is doing now, for FridayEars' island and panel
        self.speaker.on_audible = self.activity.audible
        self.conversation = Conversation(self.speaker, cfg["voice"]["hermes_url"], cfg["voice"]["conversation"],
                                         activity=self.activity, acknowledge=cfg["voice"].get("acknowledge", "voice"),
                                         narrate=bool(cfg["voice"].get("narrate", True)),
                                         context=lambda: self.brave.focus_note())
        self.registry = Registry()
        self.watcher = Watcher(self.registry, Judge(cfg["alerts"]["model"], cfg["alerts"]["effort"]),
                               self.deliver, debounce_s=cfg["alerts"]["debounce_s"])
        self.tail = TranscriptTail(self.registry, self.watcher.handle,   # works without Claude Code hooks
                                   stall_s=float(cfg["alerts"].get("stall_s", 1200)))
        self.signal = SignalWatch(self.signal_message, self.note_to_self)
        self.slack = SlackWatch(self.slack_update)
        self.brave = BraveBridge()
        self.brave.on_event = self.browser_event
        self.brave.on_pin = self.tab_pinned
        self._pin_seq = 0
        self._pin_offered: dict[str, float] = {}   # url -> when Friday last offered help on it after a pin
        self.permissions = None
        self.phone = None
        bc = cfg["branches"]
        self.branches = Branches(archive_after_days=float(bc.get("archive_after_days", 7)))
        self.router = None
        if bc.get("enabled", True):
            import anthropic
            self.router = Router(self.branches, masters=lambda: [s.title for s in self.registry.masters()],
                                 focus=self.brave.focus, client=anthropic.Anthropic(),
                                 model=bc.get("router_model", "claude-haiku-5-5"),
                                 follow_up_s=float(bc.get("follow_up_s", 120)),
                                 timeout_s=float(bc.get("classifier_timeout_s", 4.0)))
            self.conversation.router = self.router
            self.conversation.summarize = self.narrate_thought
            self.conversation.mic_open = lambda: time.time() < self.mic_open_until
            pc = cfg["phone"]
            self.phone = Phone(pc.get("ntfy_server", "https://ntfy.sh"), pc.get("ntfy_topic", ""), pc.get("reply_topic", ""),
                               pc.get("ntfy_token", ""), on_answer=self._phone_answer, on_question=self._phone_question,
                               enabled=pc.get("enabled", True))
            self.permissions = Permissions(
                say=lambda line, prio, source, wanted=None: self.speaker.say(line, prio, source=source, wanted=wanted),
                supervised=self._supervised,
                at_desk=lambda: presence.snapshot(self.cfg["presence"]["away_after_s"], max_age=5).at_desk)
            self.watcher.asking_about = self.permissions.pending_for
            if self.phone.configured:   # permission prompts reach his phone and watch when he's away
                self.permissions.notify = lambda req: self.phone.ask_permission(req.id, req.who, req.summary, req.detail)
            # Workers' failures: a note on the project's thread (no interruption); the master's next turn check sees them too.
            self.watcher.on_signal = lambda master, text: self.branches.note(self.router.project_branch(master).id, text)
            threading.Thread(target=self.router.warm, name="router-warm", daemon=True).start()
            archived = self.branches.archive_idle()
            if archived:
                log.info("archived idle threads: %s", ", ".join(archived))
        dk = cfg["ducking"]
        self.mic_open_until = 0.0   # FridayEars has the mic open (push-to-talk, follow-up): keep media down
        self.ducker = Ducker(lambda: (self.speaker.speaking is not None and not self.speaker.muted)
                             or time.time() < self.mic_open_until,
                             brave=lambda action, params: self.brave.call(action, params, timeout=5),
                             level=int(dk.get("level", 10)), restore_after_s=float(dk.get("restore_after_s", 1.2)),
                             apps=tuple(dk.get("apps", ("spotify", "music", "brave")))) if dk.get("enabled", True) else None
        self._last_offer = 0.0
        self._offered_pages: dict[str, float] = {}
        self.alerts: list[Alert] = self._load_alerts()   # the panel's Earlier list survives restarts
        self._past_turns = self._load_turns()
        self.held: list[Alert] = []
        self._lock = threading.Lock()
        self._last_greeting_day = -1
        self.signal_waiting: dict[str, int] = {}
        self.focus_held: list[Alert] = []          # routine alerts waiting for him to pause typing
        self.dispatch_names: dict[str, tuple[str, float]] = {}   # folder -> (name, when): name the next session there
        self.listen_until = 0.0                    # FridayEars keeps the mic open for an answer until then
        self._focus_since: float | None = None   # who messaged on Signal since the last greeting (counts only)
        self.quiet = self._load_prefs().get("quiet", False)
        self.speaker.muted = self.quiet
        self._slow: dict = {}                      # threads + masters, rebuilt every 1.5 s (they read files)
        self._slow_at = 0.0
        self.hub = UIHub(self.ui_state, self.ui_history)

    # --- delivery ------------------------------------------------------------------------------

    def _record(self, alert: Alert) -> None:
        with self._lock:
            self.alerts.append(alert)
            del self.alerts[:-200]
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert.as_dict(), ensure_ascii=False) + "\n")

    def _notify(self, text: str) -> None:
        script = f'display notification {json.dumps(text[:240])} with title "Friday"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)

    def _phone_answer(self, decision: str, rid: Optional[int]) -> bool:
        if not self.permissions:
            return False
        req = self.permissions.answer("deny" if decision == "deny" else "allow", rid, always=decision == "always")
        return req is not None

    def _phone_question(self, text: str) -> None:
        """Typed in the ntfy app: a turn for Friday, answered back as a notification."""
        self.conversation.ask(f"[Aman, from his phone] {text}", interrupt=False, ack=False, kind="phone",
                              title="From your phone", shown=text,
                              reply_to=lambda answer: self.phone.notify("Friday", answer, priority=3))

    def _phone(self, text: str) -> bool:
        if self.phone and self.phone.configured:
            return self.phone.notify("Friday", text, priority=4)
        cmd = self.cfg["phone"].get("command")
        if not cmd:
            return False
        argv = (cmd if isinstance(cmd, list) else cmd.split()) + [text]
        try:
            return subprocess.run(argv, capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            log.exception("phone delivery failed")
            return False

    def deliver(self, alert: Alert) -> None:
        snap = presence.snapshot(self.cfg["presence"]["away_after_s"])
        focus_kpm = int(self.cfg["presence"].get("focus_keys_per_min", 80))
        if snap.at_desk and alert.urgency != "high" and presence.typing_rate() >= focus_kpm:
            # He's typing hard: routine news waits for a pause (presence_loop releases it, at most 10 min later).
            alert.delivered = "held_focus"
            with self._lock:
                self.focus_held.append(alert)
                self._focus_since = self._focus_since or time.time()
            log.info("alert %s held while he types (%d keys/min): %s", alert.title, presence.typing_rate(), alert.line)
            self._record(alert)
            if self.router and str(alert.title).lower().startswith("master"):
                self.branches.note(self.router.project_branch(alert.title).id, f"{alert.kind}: {alert.line}")
            return
        if snap.at_desk:
            alert.delivered = "spoken"
            self.speaker.say(alert.line, alert.urgency, source=f"alert:{alert.title}",
                             on_unspoken=lambda utt, why: self._unspoken(alert))
        elif self._phone(alert.line):
            alert.delivered = "phone"
        else:
            alert.delivered = "held"
            with self._lock:
                self.held.append(alert)
        log.info("alert %s [%s] %s: %s", alert.title, alert.delivered, alert.kind, alert.line)
        self._record(alert)
        if self.router and str(alert.title).lower().startswith("master"):
            # The project's thread hears about it next time it's used, so "what happened with AWS" already knows.
            self.branches.note(self.router.project_branch(alert.title).id, f"{alert.kind}: {alert.line}")

    def signal_message(self, who: str, texts: list[str]) -> None:
        """Signal is private (someone may be in the room) and low priority: by default nothing is said
        when a message arrives, and what anyone wrote is never spoken unless Aman asks for it."""
        mode = self.cfg["signal"].get("announce", "quiet")
        if mode == "full":
            text = " / ".join(texts)
            short = text if len(text) <= 220 else text[:217] + "…"
            self.deliver(Alert(f"signal:{who}", who, "message", f"{who} on Signal: {short}", "normal"))
            return
        if mode == "quiet":
            with self._lock:
                self.signal_waiting[who] = self.signal_waiting.get(who, 0) + len(texts)
        log.info("signal: %d message(s) from %s, not announced (%s)", len(texts), who, mode)

    def note_to_self(self, text: str) -> None:
        """Aman wrote to his Signal Note to Self from his phone: a turn for Friday, answered there."""
        from friday_core import signal as signal_api

        def reply(answer: str) -> None:
            signal_api.send("me", answer)
        self.conversation.ask(f"[Aman, from his phone via Signal Note to Self] {text}", interrupt=False, ack=False,
                              kind="phone", title="From your phone", shown=text,
                              reply_to=reply)

    def slack_update(self, items: list[dict]) -> None:
        line = slack_headsup(self.watcher.judge, items) or f"{len(items)} new Slack messages for you."
        self.deliver(Alert("slack", "Slack", "message", line, "normal"))

    def _supervised(self, payload: dict) -> Optional[str]:
        """Who's asking, said naturally, if it's a session Friday looks after (a master or its worker)."""
        session = self.watcher.session_for(payload)
        if session is None or not session.title:
            return None
        if session.is_master:
            return f"The {spoken_name(session.title)} master"
        masters = {m.project_dir: m.title for m in self.registry.masters() if m.project_dir}
        if WORKER.match(session.title) and session.project_dir in masters:
            return f"{session.title} on {spoken_name(masters[session.project_dir])}"
        return None

    def session_started(self, payload: dict) -> dict:
        """SessionStart hook: name a session Friday just dispatched in this folder."""
        cwd = str(payload.get("cwd") or "")
        now = time.time()
        if payload.get("source") not in (None, "", "startup"):   # resume / clear / compact: not the new one
            return {}
        for folder, (name, at) in list(self.dispatch_names.items()):
            if now - at > 300:
                del self.dispatch_names[folder]
            elif cwd.rstrip("/") == folder.rstrip("/"):
                del self.dispatch_names[folder]
                log.info("naming the new session in %s %r", cwd, name)
                return {"title": name}
        return {}

    def browser_event(self, ev: dict) -> None:
        """The extension noticed Aman working hard on a page (minutes of reading, scrolling back,
        selecting passages). Offer help once, in a line, as a turn in the voice conversation, so his
        push-to-talk "yeah, explain the tiling part" follows on naturally."""
        pc = self.cfg["proactive"]
        if ev.get("kind") != "deep_read" or not pc.get("browser_help", True):
            return
        url, title = str(ev.get("url") or ""), str(ev.get("title") or "this page")
        now = time.time()
        if now - self._last_offer < pc.get("browser_help_cooldown_s", 1200) or now - self._offered_pages.get(url, 0) < 86400:
            return
        snap = presence.snapshot(self.cfg["presence"]["away_after_s"])
        if not snap.at_desk or snap.mic_busy or self.speaker.speaking or self.conversation.busy:
            log.info("deep read on %r: not offering now (at desk %s, mic %s, busy %s)", title[:60], snap.at_desk,
                     snap.mic_busy, self.conversation.busy or bool(self.speaker.speaking))
            return
        self._last_offer = self._offered_pages[url] = now
        mins = max(1, round(int(ev.get("active_s") or 0) / 60))
        signs = [f"scrolled back to re-read {ev['backscrolls']} times" if ev.get("backscrolls") else "",
                 f"selected passages {ev['selections']} times" if ev.get("selections") else "",
                 f"copied text {ev['copies']} times" if ev.get("copies") else ""]
        note = (f"[browser] Aman has been reading \"{title}\" in Brave for about {mins} minutes of active reading, and "
                f"{', '.join(s for s in signs if s) or 'is going over it slowly'}. He looks like he's working hard to "
                f"understand it. Offer help in one short line in your own voice, mentioning what the page is. "
                f"Don't read the page or explain anything yet. If he says yes, read the tab with brave_read "
                f"(tab_id {ev.get('tab_id')}) and help with exactly what he's stuck on.")
        log.info("deep read on %r: offering help", title[:60])
        self.conversation.ask(note, interrupt=False, ack=False, kind="offer", title="Offering help",
                              shown=f"You looked stuck on “{title[:60]}”", hint={"tab": {"url": url, "title": title}})

    def tab_pinned(self, focus: dict) -> None:
        """He pinned Friday to a tab: read it, in the tab's own thread (new, or the one it already has), and
        ask what he wants to do with it. Waits 2 s so pinning several tabs gives one offer, for the last."""
        with self._lock:
            self._pin_seq += 1
            seq = self._pin_seq
        time.sleep(2)
        if seq != self._pin_seq:
            return
        url, title = str(focus.get("url") or ""), str(focus.get("title") or "this page")
        key = url.split("#")[0]
        now = time.time()
        snap = presence.snapshot(self.cfg["presence"]["away_after_s"])
        if snap.mic_busy or self.conversation.busy or self.speaker.speaking:
            log.info("pinned %r: not offering now (mic %s, busy %s)", title[:60], snap.mic_busy,
                     self.conversation.busy or bool(self.speaker.speaking))
            return
        if now - self._pin_offered.get(key, 0) < 600:
            log.info("pinned %r again: offered on it in the last 10 min", title[:60])
            return
        self._pin_offered[key] = now
        existing = self.branches.by_link("url", key) if self.router else None
        if self.router:
            self.router._pin_routed = float(focus.get("at") or 0) / 1000   # this turn follows the pin
        history = (f"You've worked on this page with him before (this is its thread): pick up from where you "
                   f"left off rather than introducing it again. " if existing and existing.turns else "")
        note = (f"[pinned] Aman just pinned you to a tab in Brave: \"{title}\" ({url}), tab_id {focus.get('id')}. "
                f"{history}Read it with brave_read (no tab_id). Then, out loud, in one or two short sentences: what "
                f"this page is, in plain words (the gist, not a summary), and ask what he wants to do with it, "
                f"offering one or two options that fit this page. If it's private (email, chats, banking), say only "
                f"what kind of page it is: no names, amounts or message text.")
        log.info("pinned %r: reading it and asking what he wants (%s thread)", title[:60],
                 "its" if existing else "a new")
        self.conversation.ask(note, interrupt=False, ack=False, kind="offer", title="You pinned a tab",
                              shown=f"You pinned “{title[:60]}”", hint={"tab": {"url": url, "title": title}})

    def _unspoken(self, alert: Alert) -> None:
        """Mic stayed busy (a call): show it now, and repeat it in the recap."""
        alert.delivered = "notification"
        self._notify(alert.line)
        with self._lock:
            self.held.append(alert)

    def presence_loop(self) -> None:
        """Notice Aman leaving and coming back (one error never stops it)."""
        while True:
            try:
                self._presence_loop()
            except Exception:
                log.exception("presence loop failed; restarting it")
                time.sleep(5)

    def _say_recap(self, held: list[Alert]) -> None:
        """Read held alerts; if the mic stays busy (a call), they go back to held, not lost."""
        def keep(utt, why):
            with self._lock:
                self.held[:0] = held
        self.speaker.say(self.recap(held), "normal", source="recap", on_unspoken=keep)

    def _presence_loop(self) -> None:
        """Notice Aman leaving and coming back. On return, Friday speaks first, as a turn in the voice
        conversation, so his push-to-talk "yes" / "tell me more" is the natural next turn."""
        away_since = None
        while True:
            time.sleep(5)
            self._release_focus_held()
            snap = presence.snapshot(self.cfg["presence"]["away_after_s"])
            if not snap.at_desk:
                if away_since is None:
                    last_seen = max(time.time() - snap.idle_s, presence._face_seen_at)  # last key press or face
                    away_since = time.time() if snap.locked and not presence._face_seen_at else last_seen
                continue
            if away_since is None:
                if self.held and not snap.mic_busy:   # alerts held through a call: read them now
                    with self._lock:
                        held, self.held = self.held, []
                    self._say_recap(held)
                continue
            away_for, away_since = time.time() - away_since, None
            with self._lock:
                held, self.held = self.held, []
            self.greet(away_for, held)

    def greet(self, away_for: float, held: list[Alert]) -> None:
        pc = self.cfg["proactive"]
        now = time.localtime()
        first_today = self._last_greeting_day != now.tm_yday
        mins = int(away_for // 60)
        away_text = f"{mins} minutes" if mins < 120 else f"about {round(away_for / 3600, 1)} hours"
        happened = "\n".join(f"- {a.title}: {a.line}" for a in held) or "- nothing was flagged"
        with self._lock:
            waiting = dict(self.signal_waiting)
        signal_note = ("\nAlso, privately: " + ", ".join(f"{w} sent {n} Signal message{'s' if n != 1 else ''}"
                                                       for w, n in waiting.items())
                       + ". You don't know what they said and shouldn't look. At most add a few words at the end "
                         "that they messaged; never more, and skip it if the greeting is just a hello."
                       ) if waiting else ""
        if first_today and away_for >= pc["morning_after_s"] and now.tm_hour >= pc["morning_from_hour"]:
            health = latest_health()
            body = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in (health or {}).items() if k != "ts")
            note = (f"[presence] Aman just sat down at the Mac for the first time today, after {away_text} away. "
                    f"Alerts held while he was away:\n{happened}\n"
                    + (f"From his watch (Apple Health): {body}. Mention it in a few words only if something stands "
                       "out (short sleep, a high resting heart rate).\n" if body else "")
                    + 
                    "Give him his good-morning briefing: what his Claude Code masters did overnight (check them), "
                    "anything that needs him, then ask what he wants to start with. Keep it to about 30 seconds."
                    + signal_note)
        elif held and away_for >= pc["welcome_back_after_s"]:
            note = (f"[presence] Aman is back at the Mac after {away_text} away. While he was away:\n{happened}\n"
                    "Greet him in one short line, sum up what happened in a sentence or two, and ask if he wants details."
                    + signal_note)
        elif away_for >= pc["quiet_return_s"]:
            note = (f"[presence] Aman is back at the Mac after {away_text} away, and nothing was flagged. "
                    "Welcome him back in one short, easy line; don't check anything.")
        else:
            if held:  # short absence: just read what was held
                self._say_recap(held)
            return
        self._last_greeting_day = now.tm_yday
        if signal_note and signal_note in note:   # mentioned now; the short hello leaves them for a briefing
            with self._lock:
                for w, n in waiting.items():
                    left = self.signal_waiting.get(w, 0) - n
                    if left > 0:
                        self.signal_waiting[w] = left
                    else:
                        self.signal_waiting.pop(w, None)
        log.info("greeting after %ss away (%d held, first today=%s)", int(away_for), len(held), first_today)
        title = "Morning briefing" if "first time today" in note else "Welcome back"
        if not self.conversation.ask(note, interrupt=False, ack=False, kind="presence", title=title,
                                     shown="You came back to the Mac") and held:
            self._say_recap(held)

    def _release_focus_held(self) -> None:
        """Say what waited while he was typing: once he pauses (under a quarter of the focus rate), or
        after 10 minutes at most."""
        with self._lock:
            if not self.focus_held:
                return
            calm = presence.typing_rate() < int(self.cfg["presence"].get("focus_keys_per_min", 80)) // 4
            if not calm and time.time() - (self._focus_since or time.time()) < 600:
                return
            held, self.focus_held, self._focus_since = self.focus_held, [], None
        self._say_recap(held)

    @staticmethod
    def recap(held: list[Alert]) -> str:
        if len(held) == 1:
            return f"While you were away: {held[0].line}"
        lines = " ".join(a.line for a in held[-4:])
        more = f" And {len(held) - 4} more; ask me for the rest." if len(held) > 4 else ""
        return f"While you were away, {len(held)} updates. {lines}{more}"

    # --- FridayEars: quiet mode, held alerts, masters, the UI state -----------------------------

    def narrate_thought(self, request: str, notes: str) -> Optional[str]:
        """One spoken line from her working notes, or None (Haiku, ~1-2 s; skipped if slower than 4 s)."""
        if self.router is None or self.router.client is None:
            return None
        try:
            r = self.router.client.with_options(timeout=4, max_retries=0).messages.create(
                model=self.router.model, max_tokens=60, system=NARRATE_SYSTEM, thinking={"type": "disabled"},
                messages=[{"role": "user", "content": f"His request: {request[:400]}\n\nYour working notes:\n{notes}"}])
        except Exception as e:   # noqa: BLE001
            log.info("narrating a thought failed: %s", e)
            return None
        line = "".join(getattr(b, "text", "") for b in r.content).strip().strip('"“”')
        if not line or line.upper().startswith("SKIP") or len(line) > 200:
            return None
        log.info("narrating her thinking: %s", line)
        return line

    @staticmethod
    def _tail_jsonl(path: Path, n: int) -> list[dict]:
        from collections import deque
        try:
            with open(path, encoding="utf-8") as fh:
                lines = deque(fh, maxlen=n)
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _load_alerts(self) -> list[Alert]:
        fields = set(Alert.__dataclass_fields__)
        out = []
        for d in self._tail_jsonl(ALERTS_FILE, 200):
            try:
                out.append(Alert(**{k: v for k, v in d.items() if k in fields}))
            except TypeError:
                continue
        return out

    def _load_turns(self) -> list[dict]:
        """His earlier questions (before this start), for the Earlier list. Friday's own notes
        ("[presence] ...") are internal wording, so they're left out."""
        from .conversation import VOICE_LOG
        out = []
        for d in self._tail_jsonl(VOICE_LOG, 30):
            asked = str(d.get("asked") or "")
            if not asked or asked.startswith("[") or d.get("cancelled"):
                continue
            out.append({"id": f"v{int(float(d.get('ts', 0)) * 1000)}", "ts": float(d.get("ts", 0)), "kind": "turn",
                        "turn_kind": "voice", "title": "You asked", "thread": "", "text": asked,
                        "reply": str(d.get("reply") or ""), "error": d.get("error"), "cancelled": False,
                        "seconds": d.get("seconds")})
        return out

    @staticmethod
    def _load_prefs() -> dict:
        try:
            return json.loads(UI_PREFS.read_text())
        except (OSError, ValueError):
            return {}

    def set_quiet(self, on: bool) -> None:
        """Quiet mode: Friday's words appear as captions in the island, nothing is played (someone's in
        the room). Switching it on mid-sentence silences the rest of that sentence."""
        self.quiet = self.speaker.muted = bool(on)
        prefs = self._load_prefs()
        prefs["quiet"] = self.quiet
        UI_PREFS.parent.mkdir(parents=True, exist_ok=True)
        UI_PREFS.write_text(json.dumps(prefs))
        log.info("quiet mode %s", "on" if self.quiet else "off")

    def play_held(self) -> int:
        """The panel's Play: say what's been held (while he typed, or was away) right now."""
        with self._lock:
            held = self.focus_held + self.held
            self.focus_held, self.held, self._focus_since = [], [], None
        if held:
            self._say_recap(held)
        return len(held)

    @staticmethod
    def _who(title: str) -> str:
        """An alert's or session's title as a person would say it: master_aws -> AWS master."""
        if not title:
            return "Friday"
        if title.lower().startswith("master"):
            return f"{spoken_name(title)} master" if "_" in title else "Master"
        if title.startswith("signal:"):
            return title.split(":", 1)[1]
        return title

    def unspeak(self, text: Optional[str]) -> Optional[str]:
        """Captions show words as written: undo the respellings made for the voice ("G S M eight K")."""
        if not text:
            return text
        table = getattr(self, "_unspeak", None)
        if table is None:
            import re
            pairs = {spoken: written for written, spoken in self.speaker.pronunciations.items() if spoken != written}
            pattern = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, sorted(pairs, key=len, reverse=True))) + r")(?!\w)") \
                if pairs else None
            table = self._unspeak = (pattern, pairs)
        pattern, pairs = table
        return pattern.sub(lambda m: pairs[m.group(1)], text) if pattern else text

    def _source_title(self, source: str) -> str:
        if source.startswith("alert:"):
            return self._who(source[6:])
        return {"recap": "While you were away", "permission": "Permission"}.get(source, "Friday")

    def masters_view(self) -> list[dict]:
        """Each master Friday watches (active in the last 3 days): working / needs you / failed / idle,
        since when, and its latest news (the last alert about it, else its own topic line)."""
        now = time.time()
        pending = self.permissions.snapshot() if self.permissions else []
        with self._lock:
            alerts = list(self.alerts)
        order = {"needs": 0, "failed": 1, "working": 2, "idle": 3}
        out = []
        for m in self.tail.masters():
            try:
                mtime = os.stat(m.path).st_mtime
            except OSError:
                continue
            name = spoken_name(m.title or "")
            st = self.watcher.states.get(m.session_id)
            ph = self.tail.phase.get(m.path)
            quiet_since_hook = st is not None and mtime <= st.since + 3     # nothing written since that hook event
            asking = any(p["who"] == f"The {name} master" or p["who"].endswith(f" on {name}") for p in pending)
            if asking or (st and st.activity in NEEDS_YOU and quiet_since_hook and now - st.since < 6 * 3600):
                state, since = "needs", (st.since if st and st.activity in NEEDS_YOU else now)
            elif st and st.activity == "stopped on an error" and quiet_since_hook:
                state, since = "failed", st.since
            elif ph and ph[0] in ("tool", "model") and now - mtime < 6 * 3600:
                state, since = "working", ph[2] if len(ph) > 2 else mtime
            elif ph is None and now - mtime < 90:
                state, since = "working", mtime
            else:
                state, since = "idle", mtime
            news = next((a for a in reversed(alerts) if a.title == m.title and now - a.ts < 36 * 3600), None)
            out.append({"title": m.title, "name": name, "session_id": m.session_id, "state": state, "since": since,
                        "line": news.line if news else (m.ai_title or m.last_prompt or ""),
                        "line_ts": news.ts if news else None})
        out.sort(key=lambda d: (order[d["state"]], -d["since"]))
        return out

    def ui_state(self) -> dict:
        sp = self.speaker
        open_src = sp.source if sp.speaking else None
        talking = sp.talking
        saying = None
        if open_src and talking and not open_src.startswith("voice:"):
            saying = {"source": open_src, "title": self._source_title(open_src), "text": self.unspeak(sp.speaking)}
        with self._lock:
            held = [{"title": self._who(a.title), "line": a.line, "ts": a.ts} for a in self.focus_held + self.held]
        now = time.time()
        if now - self._slow_at > 1.5:
            self._slow_at = now
            try:
                self._slow = {"threads": self.branches.snapshot(), "masters": self.masters_view()}
            except Exception:
                log.exception("threads / masters for the UI failed")
        perms = self.permissions.snapshot() if self.permissions else []
        return {
            "voice": {"engine": getattr(sp.engine, "using", None), "talking": talking, "source": open_src,
                      "line": self.unspeak(sp.line) if talking else None, "quiet": self.quiet,
                      # more to say: an utterance still open (she's between sentences) or queued
                      "pending": bool(sp.speaking) or sp.queued() > 0},
            "turn": self.activity.ui_turn(open_src, talking),
            "saying": saying,
            "permissions": [{k: p[k] for k in ("id", "who", "summary", "detail", "created", "tool")} for p in perms],
            "held": held,
            "listen": now < self.listen_until or (self.permissions is not None and self.permissions.asked()),
            "judging": [{"who": self._who(t), "since": since} for t, since in list(self.watcher.judging.items())],
            "threads": self._slow.get("threads", {"current": None, "branches": []}),
            "masters": self._slow.get("masters", []),
            "browser": self.brave.connected,
        }

    def ui_history(self) -> list[dict]:
        items = self.activity.history(50) + list(self._past_turns)
        with self._lock:
            alerts = list(self.alerts[-100:])
        for a in alerts:
            items.append({"id": f"a{int(a.ts * 1000)}", "ts": a.ts, "kind": "alert", "title": self._who(a.title),
                          "text": a.line, "delivered": a.delivered})
        items.sort(key=lambda d: d["ts"], reverse=True)
        return items[:50]

    def open_session(self, session_id: str) -> None:
        from friday_core import control
        session = next((s for s in self.registry.sessions() if s.session_id == session_id), None)
        if session is None:
            log.info("open-session: no session %s", session_id)
            return
        try:
            log.info("opening %s in VS Code: %s", session.label, control.open_session(session))
        except Exception:
            log.exception("opening %s in VS Code failed", session.label)

    # --- API ------------------------------------------------------------------------------------

    def status(self) -> dict:
        snap = presence.snapshot(self.cfg["presence"]["away_after_s"], max_age=5)
        with self._lock:
            alerts = [a.as_dict() for a in self.alerts[-30:]]
            held = len(self.held)
        return {
            "presence": snap.as_dict(),
            "voice": {"engine": getattr(self.speaker.engine, "using", None),
                      "speaking": self.speaker.talking, "line": self.speaker.line,
                      "source": self.speaker.source if self.speaker.talking else None,
                      "queued": self.speaker.queued(), "thinking": self.conversation.busy},
            "sessions": {sid: {"activity": s.activity, "since": s.since, "last_event": s.last_event}
                         for sid, s in self.watcher.states.items()},
            "alerts": alerts,
            "held": held,
            "browser": self.brave.status(),
            "activity": self.activity.snapshot(self.speaker.source if self.speaker.speaking else None,
                                               self.speaker.talking, self.speaker.speaking, self.speaker.line),
            "branches": self.branches.snapshot(),
            "permissions": self.permissions.snapshot() if self.permissions else [],
            "listen": time.time() < self.listen_until or bool(self.permissions and self.permissions.pending),
        }

    def serve(self) -> None:
        friday = self
        port = urlparse(DAEMON_URL).port or 8765

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("http " + fmt, *args)

            def _client_gone(self) -> bool:
                """The caller hung up (Claude Code ended the permission hook): the socket reads EOF."""
                import select
                import socket
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True

            def _stream(self) -> None:
                """FridayEars' live state: a JSON object per data line, a heartbeat comment every 5 s."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = friday.hub.subscribe()
                try:
                    while not friday.hub.dropped(q):
                        try:
                            msg = q.get(timeout=5)
                        except queue.Empty:
                            msg = None
                        self.wfile.write(b": ping\n\n" if msg is None else b"data: " + msg.encode() + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    friday.hub.unsubscribe(q)

            def _json(self, code: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _from_web(self, post: bool) -> bool:
                """A request a web page could have made: refuse it. Friday's own callers (FridayEars, the
                hooks, the Hermes plugin) send no Origin, address 127.0.0.1, and POST JSON. A page can't send
                JSON cross-origin without a preflight (unanswered here), always sends Origin on a POST, and a
                DNS-rebinding page arrives with its own Host name."""
                host = (self.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0].strip("[]")
                if host not in ("127.0.0.1", "localhost", "::1"):
                    return True
                if self.headers.get("Origin"):
                    return True
                if post and not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return True
                return False

            def do_GET(self):
                if self._from_web(post=False):
                    return self._json(403, {"error": "forbidden"})
                if self.path.startswith("/ui/stream"):
                    return self._stream()
                if self.path.startswith("/ui"):
                    return self._json(200, friday.hub.latest())
                if self.path.startswith("/status"):
                    return self._json(200, friday.status())
                if self.path.startswith("/health"):
                    return self._json(200, {"ok": True})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if self._from_web(post=True):
                    log.warning("refused %s from a web origin (%s)", self.path, self.headers.get("Origin") or self.headers.get("Host"))
                    return self._json(403, {"error": "forbidden"})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                if self.path == "/speak":
                    ok = friday.speaker.say(str(body.get("text", "")), body.get("priority", "normal"),
                                            body.get("source", "reply"))
                    log.info("speak [%s] %d chars queued=%s", body.get("source", "reply"),
                             len(str(body.get("text", ""))), ok)
                    return self._json(200, {"queued": ok})
                if self.path == "/ask":
                    said = friday.permissions.from_speech(str(body.get("text", ""))) if friday.permissions else None
                    if said:   # "yes" / "no" / "always" to a waiting permission prompt: no model needed
                        friday.speaker.say(said, "high", source="permission")
                        return self._json(200, {"accepted": True, "handled": "permission"})
                    if body.get("typed"):   # the panel's text box: no spoken acknowledgment, he's watching the panel
                        ok = friday.conversation.ask(str(body.get("text", "")), ack=False, kind="typed", title="You typed")
                    elif body.get("reply"):   # the follow-up window: an answer to her, so it stays in her thread
                        text = str(body.get("text", ""))
                        if friday.speaker.said_recently(text):   # the mic heard her through the speaker
                            log.info("follow-up heard Friday herself, dropped: %r", text[:80])
                            return self._json(200, {"accepted": False, "echo": True})
                        ok = friday.conversation.ask(text, title="You replied", hint={"reply": True})
                    else:
                        ok = friday.conversation.ask(str(body.get("text", "")))
                    log.info("ask%s: %r accepted=%s", " (typed)" if body.get("typed") else "",
                             str(body.get("text", ""))[:80], ok)
                    return self._json(200, {"accepted": ok})
                if self.path == "/permission":   # the PermissionRequest hook: blocks until he answers (or ~110 s)
                    if not friday.permissions:
                        return self._json(200, {"decision": None})
                    # No "listen" here: most requests are for sessions Friday doesn't ask about (or its own forks);
                    # the follow-up window opens only after she has asked him (Permissions.asked).
                    return self._json(200, friday.permissions.ask(body, gone=self._client_gone))
                if self.path == "/permission/answer":   # the panel's Allow / Deny buttons, or the phone
                    req = friday.permissions.answer(str(body.get("decision", "")), body.get("id"), bool(body.get("always"))) \
                        if friday.permissions else None
                    if req:
                        friday.speaker.say("Allowed." if req.decision == "allow" else "Okay, told it no.", "high", source="permission")
                    return self._json(200, {"ok": req is not None})
                if self.path == "/session-start":   # the SessionStart hook
                    return self._json(200, friday.session_started(body))
                if self.path == "/dispatch-name":   # Friday's plugin: name the next session that starts in this folder
                    friday.dispatch_names[str(body.get("cwd", ""))] = (str(body.get("title", "")), time.time())
                    return self._json(200, {"ok": True})
                if self.path == "/branch":   # Aman picked a thread in the panel
                    b = friday.branches.pick(str(body.get("id", "")))
                    return self._json(200, {"ok": b is not None, "branch": b.title if b else None})
                if self.path == "/activity":   # Friday's plugin: a precise label for the step it's running
                    if body.get("tool") and body.get("label"):
                        friday.activity.label(str(body["tool"]), str(body["label"])[:120])
                    return self._json(200, {"ok": True})
                if self.path == "/browser":   # a command for Aman's Brave, via the Friday extension
                    action, params = str(body.get("action", "")), body.get("params") or {}
                    try:
                        result = friday.brave.call(action, params, timeout=float(body.get("timeout", 45)))
                    except BridgeError as e:
                        log.info("browser %s failed: %s", action, e)
                        return self._json(200, {"ok": False, "error": str(e), "connected": friday.brave.connected})
                    return self._json(200, {"ok": True, "result": result})
                if self.path == "/mic":   # FridayEars opened or closed the mic: music down while he talks
                    friday.mic_open_until = time.time() + 70 if body.get("open") else 0.0
                    return self._json(200, {"ok": True})
                if self.path == "/quiet":
                    friday.set_quiet(bool(body.get("on")))
                    return self._json(200, {"quiet": friday.quiet})
                if self.path == "/held/play":
                    return self._json(200, {"played": friday.play_held()})
                if self.path == "/open-session":
                    threading.Thread(target=friday.open_session, args=(str(body.get("session_id", "")),),
                                     name="open-session", daemon=True).start()
                    return self._json(200, {"ok": True})
                if self.path == "/stop":
                    playing = friday.speaker.source if friday.speaker.speaking else None
                    friday.conversation.cancel()
                    friday.activity.stop(playing)
                    return self._json(200, {"dropped": friday.speaker.stop()})
                if self.path == "/hush":   # push-to-talk: cut her voice only; his question cancels her turn
                    friday.speaker.hush()
                    return self._json(200, {"ok": True})
                if self.path == "/presence":   # FridayEars: a face is in front of the Mac / how much he's typing
                    if body.get("face"):
                        presence.saw_face()
                    if "typing" in body:
                        presence.saw_typing(body.get("typing") or 0)
                    return self._json(200, {"ok": True})
                if self.path == "/quit":   # FridayEars quitting: free the voice model's memory now
                    friday.conversation.cancel()
                    friday.speaker.stop()
                    if friday.ducker:
                        friday.ducker.restore()   # never leave his music at 10%
                    log.info("quit requested")
                    threading.Timer(0.3, lambda: os._exit(0)).start()
                    return self._json(200, {"bye": True})
                self._json(404, {"error": "not found"})

        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def orphan_watch(friday: "Friday") -> None:
    """FridayEars starts fridayd and stops everything on quit. If it vanishes without that (force quit, a
    crash), fridayd is reparented to launchd: stop what FridayEars ran (the gateway, the Signal daemon, the
    laptop voice and its tunnel) so nothing keeps running in the background, then exit. A fridayd started
    some other way (already parented to launchd) is left alone."""
    if os.getppid() == 1:
        return
    while os.getppid() != 1:
        time.sleep(3)
    log.info("FridayEars is gone (no shutdown): stopping Friday's processes")
    repo = Path(__file__).resolve().parents[2]
    friday.conversation.cancel()
    friday.speaker.stop()
    if friday.ducker:
        friday.ducker.restore()
    for argv in (["/bin/bash", str(repo / "bin/friday"), "gateway", "stop"],
                 ["/usr/bin/pkill", "-f", r"^[^ ]*/signal-cli -a [^ ]+ daemon --http 127\.0\.0\.1:8080"],
                 ["/bin/bash", str(repo / "scripts/laptop-voice.sh"), "stop"]):
        try:
            subprocess.run(argv, capture_output=True, timeout=20)
        except Exception:   # noqa: BLE001
            log.exception("stopping %s failed", argv[1])
    log.info("stopped; exiting")
    os._exit(0)


def main() -> None:
    (FRIDAY_HOME / "logs").mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(FRIDAY_HOME / "logs" / "fridayd.log", maxBytes=5 << 20, backupCount=3)
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    load_env()
    friday = Friday(load_config())
    friday.speaker.start()
    friday.watcher.start()
    friday.tail.start()
    friday.signal.start()
    friday.slack.start()
    friday.brave.start()
    if friday.phone and friday.phone.configured:
        friday.phone.start()
    if friday.ducker:
        friday.ducker.start()
    threading.Thread(target=friday.presence_loop, name="presence", daemon=True).start()
    friday.hub.start()
    threading.Thread(target=orphan_watch, args=(friday,), name="orphan-watch", daemon=True).start()
    log.info("fridayd listening on %s", DAEMON_URL)
    friday.serve()


if __name__ == "__main__":
    main()
