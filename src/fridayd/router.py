"""The root of Friday's thread tree: which thread (branch) a turn belongs to. It never answers.

Rules decide most turns instantly: explicit cues ("new topic", "back to the AWS thread"), a thread Aman
picked in the panel, one-off requests (music, the time), his focus tab ("this page"), a named Claude Code
project, and follow-ups that just continue. Ambiguous turns go to a fast classifier (Claude Haiku 5.5,
about 1.7 s through the proxy, while the acknowledgment is playing); on timeout or error the turn stays
in the current thread. Misrouting is the risk, so stickiness is the default and every switch is
announced out loud."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .branches import Branch, Branches
from .watcher import spoken_name

log = logging.getLogger("fridayd.router")

NEW_TOPIC = re.compile(r"^(?:ok(?:ay)?|so|hey|alright|right)?[\s,]*(?:new (?:topic|thread|conversation)|start (?:a )?new (?:topic|thread|conversation)|(?:a )?different (?:topic|thing))\b[\s,:.-]*(.*)$", re.I)
SWITCH = re.compile(r"\b(?:back to|switch(?:ing)? to|go(?:ing)? (?:back )?to|over to|in)\s+(?:the\s+|my\s+|our\s+)?(.{2,40}?)\s+(?:thread|topic|conversation)\b", re.I)
QUICK = re.compile(r"^(?:hey friday[\s,]*)?(?:please\s+)?(?:play\b|pause\b|resume\b|skip\b|next (?:song|track)|previous (?:song|track)|volume\b|turn (?:it |the volume )?(?:up|down)|mute\b|unmute\b|what(?:'s| is) the time|what time is it|what(?:'s| is) (?:today'?s )?date|set a timer|open [a-z ]{2,30}$|quit [a-z ]{2,30}$)", re.I)
FOLLOW = re.compile(r"^(?:and|also|what about|how about|ok(?:ay)?|so|but|why|then|wait|hmm|yeah|yes|no|nope|sure|do it|go ahead|tell me more|more|it|that|this|they|those|same|again|cool|nice|got it)\b", re.I)
FOCUS_REF = re.compile(r"\b(?:this (?:page|tab|doc|document|article|paper|post|section|part|course|chapter|thread on x|video|"
                       r"repo|repository|pr|pull request|issue|site|website|blog|tweet|email|readme|pdf|slide|deck|link)|"
                       r"the (?:page|tab|doc|article|video|repo|pr)|on my screen|what i'?m (?:reading|looking at|watching)|"
                       r"summari[sz]e (?:this|it)|explain this|what does (?:this|it) say)\b", re.I)
# A project name alone ("SGLang", "AWS") can be a technology he's asking about; with one of these it's his
# Claude Code project's work.
WORK_CUE = re.compile(r"\b(?:master\w*|sessions?|workers?|runs?|jobs?|sweep|bench(?:mark)?s?|box(?:es)?|instances?|status|progress|done|finish(?:ed)?|stuck|blocked|fail(?:ed|ing)?|errors?|doing|up to|what'?s up|how'?s|where are we|update|check on|deploy(?:ed)?|merged?|logs?|pr)\b", re.I)
COMPARE = re.compile(r"\b(?:compare|versus|vs\.?|both|between)\b", re.I)

SYSTEM = """You route each message Aman speaks to his assistant Friday into one of his conversation threads.
Each thread is a separate conversation with its own memory of what was said in it. Decide where this
message belongs.

- "stay": it continues the current thread (follow-ups, the same subject, small asides). Prefer this.
- "switch": it's clearly about the subject of another existing thread. Give its id.
- "new": a substantial new subject he'll likely talk about for a few turns (reviewing a post, learning a
  concept, planning something, a personal matter) that no thread covers. Give a short title (2-4 words,
  e.g. "X post review", "Rust ownership", "Weekend plans"), kind ("topic" for work/learning/anything, or
  "personal" for his private life), and a one-line summary.
- "quick": a one-off question or command that needs no context and no follow-up (a quick fact, a
  conversion, the weather) and would only clutter the current thread.

Personal life (relationships, family, health, feelings) never goes into a work thread, and work never
goes into "personal". If a personal thread already exists, a personal message switches to it rather
than starting another one, unless it's a clearly separate plan (a trip, an event)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["stay", "switch", "new", "quick"]},
        "branch_id": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "kind": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
    },
    "required": ["action", "branch_id", "title", "kind", "summary"],
    "additionalProperties": False,
}


@dataclass
class Route:
    branch: Branch
    action: str                  # stay | switch | new | quick | fanout | forced
    reason: str
    announce: Optional[str] = None
    fanout: list = field(default_factory=list)   # [Branch] to ask before answering here
    summary: Optional[str] = None
    prev: Optional[str] = None   # the thread that was current before this turn (restored if it's cancelled)


class Router:
    def __init__(self, branches: Branches, masters: Callable[[], list[str]], focus: Callable[[], Optional[dict]],
                 client=None, model: str = "claude-haiku-5-5", follow_up_s: float = 120, timeout_s: float = 3.0):
        self.branches, self.masters, self.focus = branches, masters, focus
        self.client, self.model = client, model
        self.follow_up_s, self.timeout_s = follow_up_s, timeout_s
        self.last_at = 0.0
        self.last_asked = False
        self._started = time.time()
        self._pin_routed = 0.0            # the pin (extension timestamp) a turn already followed
        self._last_branch: Optional[str] = None   # the thread his last turn went to (one-offs included)
        self.last_exchange: tuple[str, str] = ("", "")

    # --- projects (Claude Code masters) ---------------------------------------------------------------

    def _project_names(self) -> dict[str, str]:
        """alias -> master title ("aws" -> "master_aws")."""
        names = {}
        try:
            titles = self.masters()
        except Exception:
            titles = []
        for title in titles:
            if not title or "_" not in title:
                continue
            suffix = title.split("_", 1)[1].lower()
            for alias in {suffix, title.lower(), spoken_name(title).lower()}:
                if len(alias) >= 3:
                    names[alias] = title
        return names

    def project_branch(self, master: str) -> Branch:
        b = self.branches.by_link("master", master)
        if b is None:
            name = spoken_name(master)
            b = self.branches.create(name, "project", links={"master": master, "aliases": [master.split("_", 1)[-1]]},
                                     summary=f"the {name} project: Claude Code session {master} and its workers")
        return b

    def tab_branch(self, focus: dict) -> Branch:
        url = (focus.get("url") or "").split("#")[0]
        b = self.branches.by_link("url", url)
        if b is None:
            title = (focus.get("title") or "This tab").strip()
            b = self.branches.create(title[:40], "tab", links={"url": url}, summary=f"working through the page \"{title[:80]}\"")
        return b

    # --- the decision ---------------------------------------------------------------------------------

    def route(self, text: str, origin: str = "voice", hint: Optional[dict] = None) -> Route:
        r = self._decide(text, origin, hint or {})
        r.prev = self.branches.current
        if r.branch.kind != "quick":
            self.branches.current = r.branch.id   # now, so a reply mid-turn stays with this turn's thread
        if origin in ("voice", "typed"):
            self._last_branch = r.branch.id
        log.info("route %s -> %s (%s)", r.action, r.branch.title, r.reason)
        return r

    def _fresh_pin(self) -> Optional[dict]:
        """The tab he pinned Friday to since his last turn (not yet followed), else None."""
        focus = self.focus() or {}
        pinned = float(focus.get("at") or 0) / 1000
        if focus.get("url") and pinned > max(self._pin_routed, self._started) and pinned > self.last_at - 1:
            return focus
        return None

    def cancelled(self, route: Route) -> None:
        """A turn was cancelled before it said anything: the thread that was current before is current again."""
        if route.prev and self.branches.current == route.branch.id and route.prev != route.branch.id:
            self.branches.current = route.prev

    def _recent(self) -> Branch:
        """The thread to stay in for a follow-up: where his last turn went (even a one-off), if it was recent."""
        if self._last_branch and time.time() - self.last_at < self.follow_up_s:
            b = self.branches.get(self._last_branch)
            if b is not None:
                return b
        return self._current()

    def _current(self) -> Branch:
        return self.branches.get(self.branches.current) or self.branches.get("general")

    def _decide(self, text: str, origin: str, hint: dict) -> Route:
        t = text.strip()
        low = t.lower()
        cur = self._current()
        if hint.get("tab"):
            return Route(self.tab_branch(hint["tab"]), "forced", "help offer for the focus tab")
        if origin == "presence":
            return Route(self.branches.get("general"), "forced", "Friday speaking first")

        m = NEW_TOPIC.match(t)
        if m:
            rest = m.group(1).strip()
            guess = self._classify(rest or t, cur, force_new=True) if rest else None
            title = (guess or {}).get("title") or (" ".join(rest.split()[:4]).title() if rest else "New topic")
            kind = (guess or {}).get("kind") or "topic"
            b = self.branches.create(title, kind, summary=(guess or {}).get("summary") or "")
            return Route(b, "new", "he asked for a new topic", announce=f"New thread: {b.title}.",
                         summary=(guess or {}).get("summary"))
        m = SWITCH.search(t)
        if m:
            target = self.branches.find(m.group(1))
            if target is None:
                masters = self._named_projects(m.group(1).lower())
                target = self.project_branch(masters[0]) if len(masters) == 1 else None
            if target is not None:
                same = target.id == cur.id
                return Route(target, "stay" if same else "switch", "he named the thread",
                             announce=None if same else f"Back in the {target.title} thread.")
        # He just pinned Friday to a tab (since his last turn): this turn is about it, in its own thread. Comes
        # before the panel's manual hold: pinning is the newer thing he did.
        focus = self._fresh_pin()
        if focus:
            self._pin_routed = float(focus["at"]) / 1000
            self.branches.manual_until = 0.0
            return self._move(cur, self.tab_branch(focus), "he just pinned this tab")
        if time.time() < self.branches.manual_until:
            return Route(cur, "stay", "he picked this thread in the panel")
        if QUICK.match(t):
            return Route(self.branches.quick(), "quick", "one-off request")

        focus = self.focus() or {}
        if focus.get("url") and FOCUS_REF.search(t):
            b = self.tab_branch(focus)
            return self._move(cur, b, "his focus tab")

        named = self._named_projects(low) if WORK_CUE.search(low) else []
        if len(named) == 1:
            return self._move(cur, self.project_branch(named[0]), f"names {named[0]}")
        if len(named) >= 2 and COMPARE.search(t):
            targets = [self.project_branch(n) for n in named[:3]]
            home = cur if cur.kind in ("general", "topic") else self.branches.get("general")
            return Route(home, "fanout", "compares projects", fanout=targets,
                         announce="Checking with the " + " and ".join(b.title for b in targets) + " threads.")
        if len(named) >= 2:
            return self._move(cur, self.branches.get("general"), "several projects at once")

        if hint.get("reply"):        # the follow-up window: an answer to what she just said (after explicit cues)
            return Route(self._recent(), "stay", "his reply in the follow-up window")
        if FOLLOW.match(t) and time.time() - self.last_at < self.follow_up_s:
            return Route(self._recent(), "stay", "follow-up")
        if self.last_asked and time.time() - self.last_at < 60:
            return Route(self._recent(), "stay", "answering Friday's question")

        guess = self._classify(t, cur)
        if not guess:
            return Route(cur, "stay", "classifier unavailable")
        action = guess.get("action")
        if action == "quick":
            return Route(self.branches.quick(), "quick", "classifier: one-off")
        if action == "switch":
            target = self.branches.get(guess.get("branch_id"))
            if target is not None and target.id != cur.id:
                return Route(target, "switch", "classifier", announce=f"Back in the {target.title} thread.")
        if action == "new" and guess.get("title"):
            b = self.branches.create(guess["title"], guess.get("kind") or "topic", summary=guess.get("summary") or "")
            return Route(b, "new", "classifier", announce=f"New thread: {b.title}.", summary=guess.get("summary"))
        return Route(cur, "stay", "classifier")

    def _move(self, cur: Branch, target: Branch, reason: str) -> Route:
        if target.id == cur.id:
            return Route(cur, "stay", reason)
        verb = "Back in" if target.turns else "Starting"
        return Route(target, "switch", reason, announce=f"{verb} the {target.title} thread.")

    def _named_projects(self, low: str) -> list[str]:
        found = []
        for alias, master in sorted(self._project_names().items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", low) and master not in found:
                found.append(master)
        return found

    # --- the classifier -------------------------------------------------------------------------------

    def _classify(self, text: str, cur: Branch, force_new: bool = False) -> Optional[dict]:
        if self.client is None:
            return None
        index = [{"id": b.id, "title": b.title, "kind": b.kind, "about": b.summary, "last_used": b.last_used and
                  time.strftime("%a %H:%M", time.localtime(b.last_used))} for b in self.branches.active(12)]
        you, friday = self.last_exchange
        user = (f"Threads: {json.dumps(index, ensure_ascii=False)}\n"
                f"Current thread: {cur.id}\n"
                f"Last exchange in it: Aman: {you[:300]!r} / Friday: {friday[:300]!r}\n"
                + ("He explicitly asked for a new thread; answer with action \"new\".\n" if force_new else "")
                + f"New message: {text[:600]!r}")
        t0 = time.time()
        try:
            r = self.client.with_options(timeout=self.timeout_s, max_retries=0).messages.create(
                model=self.model, max_tokens=300, system=SYSTEM, thinking={"type": "disabled"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": user}])
            out = json.loads(next((b.text for b in r.content if b.type == "text"), "{}"))
            log.info("classifier %.1fs: %s", time.time() - t0, out)
            return out
        except Exception as e:   # noqa: BLE001 -- any failure means "stay where we are"
            log.warning("classifier failed after %.1fs: %s", time.time() - t0, e)
            return None

    def warm(self) -> None:
        """Open the classifier's connection at startup: the first call through the proxy costs ~1 s more."""
        if self.client is None:
            return
        try:
            self.client.with_options(timeout=10, max_retries=0).messages.create(
                model=self.model, max_tokens=1, thinking={"type": "disabled"},
                messages=[{"role": "user", "content": "ok"}])
        except Exception as e:   # noqa: BLE001
            log.info("classifier warm-up failed: %s", e)

    def after_turn(self, route: Route, asked: str, reply: str) -> None:
        self.last_at = time.time()
        self.last_asked = reply.rstrip().rstrip('"”').endswith("?")   # she ended on a question: his next words answer it
        self.last_exchange = (asked, reply)
        self.branches.use(route.branch.id, summary=route.summary)
