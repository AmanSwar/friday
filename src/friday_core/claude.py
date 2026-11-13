"""Read Claude Code sessions from their on-disk transcripts.

Claude Code (CLI and the VS Code extension alike) appends every session to
``~/.claude/projects/<project-slug>/<session-id>.jsonl``. Master transcripts reach 1.4 GB, so
nothing here reads a file front to back: metadata and recent turns come from the tail, read
backwards in blocks. The tail always holds the metadata because Claude Code re-appends the
``custom-title`` / ``agent-name`` / ``ai-title`` / ``last-prompt`` records every few dozen records.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .paths import CLAUDE_PROJECTS

_BLOCK = 4 << 20
_META_SCAN_BYTES = 8 << 20
_META_TYPES = {"custom-title", "agent-name", "ai-title", "last-prompt", "mode"}


def iter_lines_reversed(path: Path, max_bytes: int, block: int = _BLOCK) -> Iterator[bytes]:
    """Yield complete lines from the end of ``path`` backwards, reading at most ``max_bytes``."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        carry = b""
        read = 0
        while pos > 0 and read < max_bytes:
            n = min(block, pos)
            pos -= n
            fh.seek(pos)
            chunk = fh.read(n) + carry
            read += n
            lines = chunk.split(b"\n")
            carry = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line
        if pos == 0 and carry:
            yield carry


def _loads(line: bytes) -> Optional[dict]:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _launch_cwd(path: Path) -> Optional[str]:
    """The folder the session was started in: the first record carrying a cwd (the tail's cwd moves
    when the session cds around, e.g. master_sglang starts in its project and later cds into a repo)."""
    with open(path, "rb") as fh:
        for i, line in enumerate(fh):
            if b'"cwd"' in line:
                rec = _loads(line)
                if rec and rec.get("cwd"):
                    return rec["cwd"]
            if i > 500:
                return None
    return None


@dataclass
class Session:
    session_id: str
    path: str
    project_dir: Optional[str]    # launch folder: what the VS Code window has open
    cwd: Optional[str]            # current folder at the tail of the transcript
    title: Optional[str]          # name you gave it (custom-title), e.g. "master_aws"
    ai_title: Optional[str]       # Claude's own one-line topic
    last_prompt: Optional[str]
    git_branch: Optional[str]
    mtime: float
    size: int

    @property
    def is_master(self) -> bool:
        return bool(self.title) and self.title.lower().startswith("master")

    @property
    def project(self) -> str:
        where = self.project_dir or self.cwd
        return Path(where).name if where else Path(self.path).parent.name

    @property
    def label(self) -> str:
        return self.title or self.ai_title or self.session_id[:8]

    def brief(self) -> dict:
        d = asdict(self)
        d.update(project=self.project, is_master=self.is_master,
                 idle_seconds=int(time.time() - self.mtime))
        return d


def read_session_meta(path: Path) -> Session:
    st = path.stat()
    found: dict = {}
    cwd = branch = None
    for line in iter_lines_reversed(path, _META_SCAN_BYTES):
        rec = _loads(line)
        if rec is None:
            continue
        kind = rec.get("type")
        if kind in _META_TYPES and kind not in found:
            found[kind] = rec
        if cwd is None and rec.get("cwd"):
            cwd, branch = rec["cwd"], rec.get("gitBranch")
        if cwd and {"custom-title", "ai-title", "last-prompt"} <= found.keys():
            break
    title = (found.get("custom-title") or {}).get("customTitle") or (found.get("agent-name") or {}).get("agentName")
    return Session(
        session_id=path.stem,
        path=str(path),
        project_dir=_launch_cwd(path),
        cwd=cwd,
        title=title,
        ai_title=(found.get("ai-title") or {}).get("aiTitle"),
        last_prompt=(found.get("last-prompt") or {}).get("lastPrompt"),
        git_branch=branch,
        mtime=st.st_mtime,
        size=st.st_size,
    )


@dataclass
class Registry:
    """Session metadata, cached per file and refreshed when the file changes."""

    root: Path = CLAUDE_PROJECTS
    max_age_days: float = 30
    _cache: dict = field(default_factory=dict)

    def sessions(self) -> list[Session]:
        cutoff = time.time() - self.max_age_days * 86400
        out = []
        for path in self.root.glob("*/*.jsonl"):
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = (st.st_size, st.st_mtime)
            cached = self._cache.get(path)
            if cached is None or cached[0] != key:
                try:
                    cached = (key, read_session_meta(path))
                except OSError:
                    continue
                self._cache[path] = cached
            out.append(cached[1])
        out.sort(key=lambda s: s.mtime, reverse=True)
        return out

    def masters(self) -> list[Session]:
        """The newest session for each master title (a title can survive across resumed copies)."""
        seen: dict[str, Session] = {}
        for s in self.sessions():
            if s.is_master and s.title not in seen:
                seen[s.title] = s
        return list(seen.values())

    def by_path(self, transcript_path: str) -> Optional[Session]:
        path = Path(transcript_path)
        try:
            return read_session_meta(path)
        except OSError:
            return None

    def resolve(self, ref: str, masters_only: bool = False) -> list[Session]:
        """Match a loose reference ("aws", "master_sglang", a session-id prefix, "trainium")."""
        pool = self.masters() if masters_only else self.sessions()
        ref_l = ref.strip().lower()
        if not ref_l:
            return []
        exact = [s for s in pool if s.session_id.startswith(ref_l) or (s.title or "").lower() == ref_l]
        if exact:
            return exact
        words = [w for w in re.split(r"[\s_\-/]+", ref_l) if w and w not in {"the", "one", "master", "project"}]

        def score(s: Session) -> int:
            hay = " ".join(filter(None, [s.title, s.ai_title, s.project_dir, s.cwd, s.last_prompt,
                                         s.git_branch])).lower()
            return sum(1 for w in words if w in hay)

        ranked = sorted(((score(s), s) for s in pool), key=lambda t: (t[0], t[1].mtime), reverse=True)
        best = ranked[0][0] if ranked else 0
        return [s for sc, s in ranked if best and sc == best]


# --- recent turns -------------------------------------------------------------------------------

def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _tool_summary(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    for key in ("command", "description", "file_path", "path", "pattern", "url", "query", "prompt", "message"):
        if inp.get(key):
            return f"{name}: {_clip(inp[key], 160)}"
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        return "AskUserQuestion: " + " | ".join(_clip(q.get("question", ""), 160) for q in qs)
    return name


def _prompt_text(rec: dict) -> Optional[str]:
    """The text of a prompt record (typed, peer or scheduled); None for tool results and wrappers."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    if not text.strip() or text.startswith("<"):  # <command-name>, <local-command-stdout>, hook wrappers
        return None
    return text


_PEER_RE = re.compile(r'^Another Claude session sent a message:\s*<[\w-]+ [^>]*?from-name="([^"]+)"[^>]*>\s*', re.S)
_PEER_ANY_RE = re.compile(r"^Another Claude session sent a message:\s*<[^>]*>\s*", re.S)


def turn_origin(rec: dict) -> str:
    """Who started this prompt: "human" (you typed it), "peer" (another session), or "scheduled"."""
    origin = rec.get("turnOrigin")
    if origin in ("human", "peer", "scheduled"):
        return origin
    text = _prompt_text(rec) or ""
    return "peer" if text.startswith("Another Claude session sent a message") else "human"


def describe_prompt(rec: dict, limit: int = 1500) -> Optional[str]:
    """One digest line for a prompt, labelled by who sent it."""
    text = _prompt_text(rec)
    if text is None:
        return None
    origin = turn_origin(rec)
    if origin == "peer":
        m = _PEER_RE.match(text)
        sender = m.group(1) if m else "another session"
        body = _PEER_ANY_RE.sub("", text, count=1)
        return f"{sender.upper()} → this session: {_clip(body, limit)}"
    if origin == "scheduled":
        return f"SCHEDULED PROMPT: {_clip(text, min(limit, 400))}"
    return f"YOU: {_clip(text, limit)}"


def recent_records(path: Path, human_turns: int = 3, max_prompts: int = 30,
                   max_bytes: int = 48 << 20) -> list[dict]:
    """Main-thread user/assistant records, oldest first, reaching back over the last ``human_turns``
    prompts you typed or ``max_prompts`` prompts of any origin, whichever comes first."""
    out: list[dict] = []
    human = prompts = 0
    for line in iter_lines_reversed(path, max_bytes):
        if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
            continue
        rec = _loads(line)
        if rec is None or rec.get("type") not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        out.append(rec)
        if rec["type"] == "user" and _prompt_text(rec) is not None:
            prompts += 1
            human += turn_origin(rec) == "human"
            if human >= human_turns or prompts >= max_prompts:
                break
    out.reverse()
    return out


def render_digest(records: list[dict], budget: int = 14000) -> str:
    """Compact transcript text for an LLM: prompts and replies in full, tool calls one line each."""
    lines: list[str] = []
    for rec in records:
        msg = rec.get("message") or {}
        content = msg.get("content")
        ts = (rec.get("timestamp") or "")[11:16]
        if rec["type"] == "user":
            line = describe_prompt(rec)
            if line:
                lines.append(f"[{ts}] {line}")
                continue
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result":
                    body = b.get("content")
                    if isinstance(body, list):
                        body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                    flag = " (error)" if b.get("is_error") else ""
                    lines.append(f"      ↳ result{flag}: {_clip(body or '', 220)}")
        else:
            for b in content or []:
                kind = b.get("type")
                if kind == "text" and b.get("text", "").strip():
                    lines.append(f"[{ts}] CLAUDE: {_clip(b['text'], 2500)}")
                elif kind == "tool_use":
                    lines.append(f"      → {_tool_summary(b)}")
    text = "\n".join(lines)
    if len(text) > budget:  # keep the end: the newest turns matter most
        text = "…(earlier turns trimmed)…\n" + text[-budget:]
    return text


@dataclass
class TurnSummary:
    origin: str              # who started the turn that just ended: human | peer | scheduled
    prompt: str              # labelled prompt line
    reply: str               # the assistant's final text in that turn
    tools: list[str]         # one line per tool call made during the turn


def last_turn(records: list[dict]) -> Optional[TurnSummary]:
    """The most recent prompt and everything Claude did after it."""
    start = None
    for i in range(len(records) - 1, -1, -1):
        rec = records[i]
        if rec["type"] == "user" and _prompt_text(rec) is not None:
            start = i
            break
    if start is None:
        return None
    tail = records[start + 1:]
    tools = [_tool_summary(b) for r in tail if r["type"] == "assistant"
             for b in (r.get("message") or {}).get("content") or [] if b.get("type") == "tool_use"]
    return TurnSummary(origin=turn_origin(records[start]), prompt=describe_prompt(records[start], 600) or "",
                       reply=last_assistant_text(tail), tools=tools)


def last_assistant_text(records: list[dict]) -> str:
    for rec in reversed(records):
        if rec["type"] != "assistant":
            continue
        texts = [b.get("text", "") for b in (rec.get("message") or {}).get("content") or []
                 if b.get("type") == "text" and b.get("text", "").strip()]
        if texts:
            return "\n".join(texts)
    return ""


def infer_activity(records: list[dict], mtime: float) -> str:
    """Best guess from the transcript alone; hook events (fridayd) refine it."""
    if not records:
        return "unknown"
    age = time.time() - mtime
    last = records[-1]
    blocks = (last.get("message") or {}).get("content") or []
    if last["type"] == "assistant" and any(b.get("type") == "tool_use" for b in blocks if isinstance(b, dict)):
        return "running a tool" if age < 600 else "stalled on a tool call (or waiting for permission)"
    if last["type"] == "user":
        return "working" if age < 600 else "stopped mid-turn"
    return "finished its turn, waiting for you" if age > 20 else "just replied"
