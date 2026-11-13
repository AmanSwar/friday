"""Friday's threads ("branches"): one persistent Hermes session per topic, so a question about the AWS
master never shares a context with an X post review or a Trainium course.

The registry lives in ~/.friday/state/branches.json. Each branch maps to a Hermes session id; the
general branch keeps the original "friday-voice" session, the rest are "friday-b-<slug>-<4 hex>"
(Hermes gives every friday-b-* session the 1h cache TTL). Branches never have earlier content
edited: anything Friday wants a branch to know (alerts for its project, the other threads, the focus
tab) goes into the next user message, so each branch's prompt cache stays a pure append."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from friday_core.paths import STATE_DIR

REGISTRY = STATE_DIR / "branches.json"
GENERAL_SESSION = "friday-voice"
KINDS = ("general", "project", "tab", "topic", "personal", "quick")
MAX_NOTES = 12


@dataclass
class Branch:
    id: str
    title: str
    kind: str
    session: str
    created: float = field(default_factory=time.time)
    last_used: float = 0.0
    turns: int = 0
    summary: str = ""            # one line: what this thread is about (shown to the router and other threads)
    links: dict = field(default_factory=dict)   # {"master": "master_aws"} | {"url": "..."} | {"day": "2026-11-11"}
    status: str = "active"       # active | archived
    notes: list = field(default_factory=list)   # [ts, text] queued for the next turn here


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "thread"


def ago(ts: float) -> str:
    if not ts:
        return "not yet used"
    s = time.time() - ts
    if s < 120:
        return "just now"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    d = int(s // 86400)
    return "yesterday" if d == 1 else f"{d} days ago"


class Branches:
    def __init__(self, archive_after_days: float = 7):
        self._lock = threading.RLock()
        self.archive_after_s = archive_after_days * 86400
        self.items: dict[str, Branch] = {}
        self.current: Optional[str] = None
        self.manual_until = 0.0          # Aman picked a thread in the panel: stay there for a while
        self._load()
        if "general" not in self.items:
            self.items["general"] = Branch("general", "General", "general", GENERAL_SESSION,
                                           summary="everything that isn't its own thread yet")
            self._save()

    # --- persistence --------------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(REGISTRY.read_text())
        except (OSError, ValueError):
            return
        for raw in data.get("branches", []):
            try:
                b = Branch(**raw)
            except TypeError:
                continue
            self.items[b.id] = b
        self.current = data.get("current")

    def _save(self) -> None:
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY.with_suffix(".tmp")
        tmp.write_text(json.dumps({"current": self.current, "branches": [asdict(b) for b in self.items.values()]},
                                  ensure_ascii=False, indent=1))
        tmp.replace(REGISTRY)

    # --- lookups ------------------------------------------------------------------------------------

    def get(self, bid: Optional[str]) -> Optional[Branch]:
        with self._lock:
            return self.items.get(bid) if bid else None

    def by_link(self, key: str, value: str) -> Optional[Branch]:
        with self._lock:
            return next((b for b in self.items.values() if b.links.get(key) == value), None)

    def find(self, words: str) -> Optional[Branch]:
        """A thread named in speech ("the Trainium thing", "AWS"): best title or alias overlap."""
        want = set(re.findall(r"[a-z0-9]+", words.lower())) - {"the", "a", "my", "thread", "topic", "thing", "one", "about", "on"}
        if not want:
            return None
        best, score = None, 0
        with self._lock:
            for b in self.items.values():
                if b.kind == "quick":
                    continue
                names = set(re.findall(r"[a-z0-9]+", f"{b.title} {b.links.get('master', '')} {' '.join(b.links.get('aliases', []))}".lower()))
                s = len(want & names)
                if s > score or (s == score and s and b.last_used > (best.last_used if best else 0)):
                    best, score = b, s
        return best if score else None

    def active(self, limit: int = 12) -> list[Branch]:
        with self._lock:
            live = [b for b in self.items.values() if b.status == "active" and b.kind != "quick"]
        return sorted(live, key=lambda b: b.last_used or b.created, reverse=True)[:limit]

    # --- changes ------------------------------------------------------------------------------------

    def create(self, title: str, kind: str, links: Optional[dict] = None, summary: str = "") -> Branch:
        kind = kind if kind in KINDS else "topic"
        with self._lock:
            base = slug(title)
            bid = base
            n = 2
            while bid in self.items:
                bid = f"{base}-{n}"
                n += 1
            b = Branch(bid, title.strip()[:60] or "New thread", kind, f"friday-b-{base}-{secrets.token_hex(2)}",
                       links=dict(links or {}), summary=summary[:160])
            self.items[bid] = b
            self._save()
            return b

    def quick(self) -> Branch:
        """One-shot requests (music, volume, the time) share a throwaway thread that starts fresh daily."""
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            b = next((x for x in self.items.values() if x.kind == "quick" and x.links.get("day") == day), None)
            if b is None:
                for old in [x for x in self.items.values() if x.kind == "quick"]:
                    del self.items[old.id]
                b = Branch(f"quick-{day}", "Quick", "quick", f"friday-b-quick-{day.replace('-', '')}", links={"day": day},
                           summary="one-off requests: music, volume, time, quick notes")
                self.items[b.id] = b
                self._save()
            return b

    def use(self, bid: str, summary: Optional[str] = None) -> None:
        with self._lock:
            b = self.items.get(bid)
            if b is None:
                return
            b.last_used = time.time()
            b.turns += 1
            b.status = "active"
            if summary:
                b.summary = summary[:160]
            if b.kind != "quick":
                self.current = bid
            self._save()

    def pick(self, bid: str, hold_s: float = 900) -> Optional[Branch]:
        """Aman chose a thread himself (panel tap, "switch to X"): the next turns go there."""
        with self._lock:
            b = self.items.get(bid)
            if b is None:
                return None
            b.status = "active"
            self.current = bid
            self.manual_until = time.time() + hold_s
            self._save()
            return b

    def note(self, bid: str, text: str) -> None:
        """Something this thread should know next time it's used (an alert from its project)."""
        with self._lock:
            b = self.items.get(bid)
            if b is None:
                return
            b.notes = (b.notes + [[time.time(), text[:300]]])[-MAX_NOTES:]
            self._save()

    def take_notes(self, bid: str) -> list:
        with self._lock:
            b = self.items.get(bid)
            if b is None or not b.notes:
                return []
            notes, b.notes = b.notes, []
            self._save()
            return notes

    def archive_idle(self) -> list[str]:
        cutoff = time.time() - self.archive_after_s
        done = []
        with self._lock:
            for b in self.items.values():
                if b.status == "active" and b.kind not in ("general", "quick") and (b.last_used or b.created) < cutoff:
                    b.status = "archived"
                    done.append(b.title)
            if done:
                self._save()
        return done

    # --- what each turn carries ---------------------------------------------------------------------

    def context(self, branch: Branch, others: int = 6) -> str:
        """The thread line that starts each user message: where we are, what else is open, and anything
        that happened in this thread's world since it was last used."""
        rest = [b for b in self.active(others + 1) if b.id != branch.id][:others]
        lines = [f"[Thread: {branch.title} ({branch.kind}). Other threads: "
                 + (", ".join(f"{b.title} ({ago(b.last_used)})" for b in rest) or "none") + ".]"]
        notes = self.take_notes(branch.id)
        if notes:
            lines.append("[Since you were last in this thread: "
                         + " / ".join(f"{time.strftime('%H:%M', time.localtime(ts))} {text}" for ts, text in notes) + "]")
        return "\n".join(lines)

    def snapshot(self) -> dict:
        with self._lock:
            cur = self.current
        return {"current": cur, "branches": [{"id": b.id, "title": b.title, "kind": b.kind, "last_used": b.last_used,
                                               "ago": ago(b.last_used), "turns": b.turns, "current": b.id == cur}
                                              for b in self.active(10)]}
