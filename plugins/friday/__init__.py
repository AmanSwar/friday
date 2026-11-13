"""Friday's Hermes plugin: Claude Code tools for the agent, and spoken replies on the Mac.

Session discovery and control live in friday_core (repo src/, stdlib only); the voice lives in
fridayd, reached over 127.0.0.1. This file only adapts them to Hermes tools and hooks.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from friday_core import control  # noqa: E402
from friday_core import mac  # noqa: E402
from friday_core import signal as signal_api  # noqa: E402
from friday_core import signal_desktop  # noqa: E402
from friday_core import slack as slack_api  # noqa: E402
from friday_core.claude import (Registry, infer_activity, last_turn, recent_records,  # noqa: E402
                                render_digest)
from friday_core.paths import ALERTS_FILE, DAEMON_URL, STATE_DIR  # noqa: E402

log = logging.getLogger("friday.plugin")
_registry = Registry()
_LOCAL_SURFACES = {"cli", "tui", "desktop"}
_MUTE_FLAG = STATE_DIR / "replies-muted"
# Hermes passes the X-Hermes-Session-Id as session_id: fridayd's voice and phone turns use "friday-voice";
# cron jobs and other clients have their own, and their steps must not label the voice turn's.
_VOICE_SESSIONS = ("friday-voice",)


def _is_voice(session_id) -> bool:
    """fridayd's voice turns: the general thread ("friday-voice") and every thread session ("friday-b-...")."""
    sid = str(session_id or "")
    return sid in _VOICE_SESSIONS or sid.startswith("friday-b-")


def _daemon(path: str, body: dict | None = None, timeout: float = 3) -> dict | None:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DAEMON_URL + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except (OSError, ValueError):
        return None


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False, default=str)


def _err(message: str, **payload) -> str:
    return json.dumps({"success": False, "error": message, **payload}, ensure_ascii=False, default=str)


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def _resolve(ref: str):
    hits = _registry.resolve(ref, masters_only=True) or _registry.resolve(ref)
    if len(hits) > 1 and not any((h.title or "").lower() == ref.strip().lower() for h in hits):
        return None, [h.title or h.session_id[:8] for h in hits[:6]]
    return (hits[0] if hits else None), []


def _activity(session, records=None) -> str:
    live = ((_daemon("/status") or {}).get("sessions") or {}).get(session.session_id)
    if live:
        return f"{live['activity']} ({_ago(time.time() - live['since'])})"
    records = records if records is not None else recent_records(Path(session.path), human_turns=1, max_prompts=2)
    return infer_activity(records, session.mtime)


# --- tools --------------------------------------------------------------------------------------

def claude_sessions(params, **_):
    include_all = bool(params.get("include_workers"))
    pool = _registry.sessions() if include_all else _registry.masters()
    open_uris = set(control.open_windows())
    rows = []
    for s in pool[:25]:
        rows.append({
            "name": s.title or s.ai_title or s.session_id[:8],
            "session_id": s.session_id,
            "project": s.project,
            "topic": s.ai_title,
            "your_last_prompt": (s.last_prompt or "")[:200],
            "last_activity": _ago(time.time() - s.mtime),
            "activity": _activity(s),
            "window_open": control.window_for(s) in open_uris if open_uris else None,
        })
    return _ok(sessions=rows)


def claude_status(params, **_):
    session, ambiguous = _resolve(params.get("session", ""))
    if session is None:
        return _err("ambiguous session" if ambiguous else "no such session", candidates=ambiguous)
    records = recent_records(Path(session.path), human_turns=int(params.get("depth", 3)))
    alerts = []
    if ALERTS_FILE.exists():
        for line in ALERTS_FILE.read_text().splitlines()[-200:]:
            try:
                a = json.loads(line)
            except ValueError:
                continue
            if a.get("session_id") == session.session_id:
                alerts.append({"when": _ago(time.time() - a["ts"]), "said": a["line"]})
    return _ok(
        name=session.title, project=session.project, topic=session.ai_title,
        activity=_activity(session, records),
        your_last_prompt=session.last_prompt,
        recent_alerts=alerts[-5:],
        transcript=render_digest(records, budget=int(params.get("budget", 14000))),
    )


def claude_ask(params, **_):
    session, ambiguous = _resolve(params.get("session", ""))
    if session is None:
        return _err("ambiguous session" if ambiguous else "no such session", candidates=ambiguous)
    question = (params.get("question") or "").strip()
    if not question:
        return _err("question is required")
    result = control.ask(session, question)
    return _ok(name=session.title, **result) if result.get("ok") else _err(result.get("error", "failed"))


def claude_send(params, **_):
    session, ambiguous = _resolve(params.get("session", ""))
    if session is None:
        return _err("ambiguous session" if ambiguous else "no such session", candidates=ambiguous)
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return _err("prompt is required")
    result = control.send(session, prompt, submit=params.get("submit", True) is not False)
    if result.pop("ok", False):
        return _ok(name=session.title, **result)
    return _err(result.pop("error", "failed"), name=session.title, **result)


PERSONAL = Path.home() / "code" / "personal"


def _areas() -> dict[str, Path]:
    """Aman's personal-notes areas (folders with a memory.md), excluding Friday's own and the archive."""
    if not PERSONAL.is_dir():
        return {}
    return {d.name: d / "memory.md" for d in sorted(PERSONAL.iterdir())
            if d.is_dir() and d.name not in ("friday", "_archive") and (d / "memory.md").is_file()}


def _area_names() -> str:
    """For the tool description: his areas as they are on this Mac (the source names none)."""
    try:
        return ", ".join(_areas()) or "none yet"
    except Exception:
        return "the folders in ~/code/personal"


def _vault_names() -> str:
    try:
        vs = mac.vaults()
        return ", ".join(f"'{v['name']}'" + (" (default)" if i == 0 else "") for i, v in enumerate(vs)) or "none found"
    except Exception:
        return "his vaults"


def personal_notes(params, **_):
    areas = _areas()
    if not areas:
        return _err("Aman's personal notes aren't available (~/code/personal).")
    area = str(params.get("area") or "").strip().lower().replace(" ", "-")
    query = str(params.get("query") or "").strip()
    if area:
        match = areas.get(area) or next((p for name, p in areas.items() if area in name), None)
        if match is None:
            return _err(f"No area {area!r}.", areas=list(areas))
        text = match.read_text(errors="replace")
        return _ok(area=match.parent.name, text=text[:14000], truncated=len(text) > 14000)
    if query:
        words = [w for w in query.lower().split() if len(w) > 2]
        need = max(1, len(words) - 1)          # most of the words, counting the line's section and parent bullet
        hits = []
        for name, path in areas.items():
            heading = parent = ""
            for line in path.read_text(errors="replace").splitlines():
                if line.startswith("#"):
                    heading, parent = line.lstrip("# ").strip(), ""
                    continue
                if line.startswith("- "):
                    parent = line
                context = f"{name} {heading} {parent if line.startswith(' ') else ''} {line}".lower()
                score = sum(w in context for w in words)
                if line.strip() and score >= need:
                    hits.append((score, {"area": name, "section": heading, "line": line.strip()[:400]}))
        hits.sort(key=lambda h: -h[0])
        return _ok(query=query, matches=[h for _, h in hits[:20]], total=len(hits))
    return _ok(areas={name: path.stat().st_size for name, path in areas.items()},
               hint="Read an area with area=..., or search them all with query=....")


def health_recent(params, **_):
    path = STATE_DIR / "health.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        rows = []
    days = int(params.get("days") or 7)
    rows = [r for r in rows if time.time() - r.get("ts", 0) < days * 86400]
    if not rows:
        return _err("No health data from his phone yet (the iPhone Shortcut from scripts/phone-setup.py sends it).")
    return _ok(days=days, summaries=rows[-days * 2:])


def claude_dispatch(params, **_):
    spoken = str(params.get("folder") or "").strip()
    folder, others = control.find_folder(spoken)
    if folder is None:
        if others:
            return _err(f"More than one folder matches {spoken!r}; ask Aman which one.", candidates=others)
        return _err(f"No folder matching {spoken!r} under ~/code. Ask Aman for the path.")
    name = str(params.get("name") or "").strip()
    if name:   # the SessionStart hook names the session that starts in this folder next
        _daemon("/dispatch-name", {"cwd": folder, "title": name})
    result = control.dispatch(folder, str(params.get("prompt") or ""), submit=params.get("submit") is not False)
    return _ok(**result, name=name or None, next="Talk to it with claude_send using its name"
               + ("" if name else " (it gets Claude Code's automatic title)") + ".")


def friday_alerts(params, **_):
    limit = int(params.get("limit", 10))
    rows = []
    if ALERTS_FILE.exists():
        for line in ALERTS_FILE.read_text().splitlines()[-limit:]:
            try:
                a = json.loads(line)
            except ValueError:
                continue
            if str(a.get("session_id", "")).startswith("signal:"):
                continue   # Signal is private: what people wrote isn't part of "what did I miss"
            rows.append({"when": _ago(time.time() - a["ts"]), "session": a["title"], "kind": a["kind"],
                         "said": a["line"], "delivered": a.get("delivered")})
    status = _daemon("/status")
    return _ok(alerts=rows, daemon_running=status is not None,
               presence=(status or {}).get("presence"), held_for_recap=(status or {}).get("held"))


def friday_hush(params, **_):
    result = _daemon("/stop", {})
    return _ok(dropped=(result or {}).get("dropped", 0)) if result is not None else _err("fridayd is not running")


def _slack(fn):
    try:
        return _ok(**fn(slack_api.Slack()))
    except slack_api.SlackError as e:
        return _err(str(e))


def slack_catchup(params, **_):
    hours = float(params.get("hours", 12))
    return _slack(lambda s: s.catchup(hours=hours))


def slack_read(params, **_):
    return _slack(lambda s: s.read(params.get("channel", ""), hours=float(params.get("hours", 24))))


def slack_search(params, **_):
    return _slack(lambda s: s.search(params.get("query", ""), count=int(params.get("count", 20))))


_consulted: dict[tuple[str, str], float] = {}   # (tool, contact) -> when Friday last read it


def _mark(tool: str, name: str) -> None:
    _consulted[(tool, name.strip().lower())] = time.time()


def _read_recently(tool: str, name: str, within: float = 900) -> bool:
    return time.time() - _consulted.get((tool, name.strip().lower()), 0) < within


_SELF_NAMES = {"me", "myself", "self", "note to self", "aman"}


def _to_self(key: str) -> bool:
    if key in _SELF_NAMES:
        return True
    try:
        return key == str(signal_api.account() or "").lower()
    except Exception:  # noqa: BLE001 -- unknown account: the name check above still applies
        return False


def signal_send(params, session_id="", **_):
    to = params.get("to", "")
    key = to.strip().lower()
    if _to_self(key) and not _is_voice(session_id):
        # Aman asked (8 Nov 2026): scheduled jobs never write to his Note to Self. Voice/phone turns still can.
        return _err("Not sent. Aman doesn't want Note to Self messages from scheduled jobs; put it in your final "
                    "response instead.")
    if signal_api.style_guide(key) and not (_read_recently("signal_recent", key) and _read_recently("contact_style", key)):
        return _err(f"Not sent. Before writing to {to}, call signal_recent and contact_style for them, then write "
                    "the message as Aman would, continuing that chat. Never mention Friday or that it's a test.")
    try:
        r = signal_api.send(to, params.get("text", ""), confirmed=params.get("confirmed") is True)
    except (signal_api.SignalError, OSError) as e:
        return _err(str(e))
    if r.get("needs_confirmation"):
        return _ok(**r, instruction="Not sent. Read Aman the recipient and the exact text, and call again with "
                                    "confirmed=true only after he says yes.")
    return _ok(**r)


def signal_recent(params, **_):
    try:
        result = signal_desktop.recent(params.get("name", ""), int(params.get("count", 30)))
        _mark("signal_recent", params.get("name", ""))
        return _ok(**result)
    except (signal_desktop.SignalDesktopError, OSError, ValueError) as e:
        return _err(str(e))


def contact_style(params, **_):
    guide = signal_api.style_guide(params.get("name", ""))
    if guide:
        _mark("contact_style", params.get("name", ""))
    return _ok(guide=guide) if guide else _err("no style guide for that person; write naturally in Aman's voice")


# --- Aman's own Brave (the Friday extension, via fridayd) -----------------------------------------

def _brave(action: str, params: dict, timeout: float = 50) -> str:
    res = _daemon("/browser", {"action": action, "params": {k: v for k, v in params.items() if v is not None},
                               "timeout": timeout}, timeout=timeout + 5)
    if res is None:
        return _err("fridayd isn't running, so Brave can't be reached.")
    if not res.get("ok"):
        hint = "" if res.get("connected", True) else (
            " Aman needs Brave open with the Friday extension loaded (brave://extensions, Load unpacked: "
            "~/code/mine/friday/extensions/brave). For signed-out pages, browser_exec still works.")
        return _err(str(res.get("error")) + hint)
    return _ok(**res.get("result", {}))


def brave_open(params, **_):
    if not params.get("url") and not params.get("search"):
        return _err("give a url, or search (words to look up on Google)")
    return _brave("open", {"url": params.get("url"), "search": params.get("search"), "show": bool(params.get("show")),
                           "tab_id": params.get("tab_id"), "scroll": params.get("scroll"),
                           "links": params.get("links"), "selector": params.get("selector"),
                           "max_chars": params.get("max_chars")})


def brave_read(params, **_):
    return _brave("read", {"tab_id": params.get("tab_id"), "scroll": params.get("scroll"), "links": params.get("links"),
                           "selector": params.get("selector"), "max_chars": params.get("max_chars")})


def brave_tabs(params, **_):
    return _brave("tabs", {}, timeout=10)


def brave_click(params, **_):
    if not params.get("text") and not params.get("selector"):
        return _err("give the visible text or a CSS selector to click")
    return _brave("click", {"tab_id": params.get("tab_id"), "text": params.get("text"),
                            "selector": params.get("selector"), "scroll": params.get("scroll"),
                            "links": params.get("links")})


def brave_focus(params, **_):
    op = str(params.get("op") or "get")
    return _brave("focus", {"op": op, "tab_id": params.get("tab_id")}, timeout=10)


def brave_close(params, **_):
    return _brave("close", {"tab_id": params.get("tab_id")}, timeout=10)


# --- the Mac: apps, volume, Spotify, Apple Notes, Obsidian ------------------------------------------

def _mac(fn, *args, **kwargs) -> str:
    try:
        return _ok(**fn(*args, **kwargs))
    except mac.MacError as e:
        return _err(str(e))


def mac_open(params, **_):
    return _mac(mac.open_target, str(params.get("target", "")))


def mac_quit(params, **_):
    return _mac(mac.quit_app, str(params.get("app", "")))


def mac_volume(params, **_):
    level = params.get("level")
    return _mac(mac.volume, int(level) if level is not None else None, params.get("mute"))


def _find_on_spotify(query: str, kind: str) -> str | None:
    """A Spotify link for a spoken request, via Hermes's keyless web search (no Spotify app or login)."""
    from tools.web_tools import web_search_tool
    raw = web_search_tool(f"{query} {kind if kind != 'track' else 'song'} site:open.spotify.com", limit=8)
    found = [(m.group(1), f"spotify:{m.group(1)}:{m.group(2)}") for m in mac.SPOTIFY_LINK.finditer(raw)]
    if not found:
        return None
    return next((uri for k, uri in found if k == kind), found[0][1])


def spotify(params, **_):
    action = str(params.get("action", "now"))
    if action == "play" and (params.get("query") or params.get("link")):
        link = params.get("link")
        uri = mac.spotify_uri(link) if link else None
        if not uri and params.get("query"):
            try:
                uri = _find_on_spotify(str(params["query"]), str(params.get("kind") or "track"))
            except Exception as e:  # noqa: BLE001 -- search is best-effort; say why it failed
                return _err(f"couldn't search for that: {e}")
        if not uri:
            return _err("couldn't find that on Spotify")
        return _mac(mac.spotify, "play_uri", uri=uri)
    level = params.get("level")
    return _mac(mac.spotify, action, level=int(level) if level is not None else None)


def apple_notes(params, **_):
    action = str(params.get("action", ""))
    title, text = str(params.get("title") or ""), str(params.get("text") or "")
    if action == "create":
        if not text and not title:
            return _err("nothing to write")
        return _mac(mac.notes_create, title or text.split("\n")[0][:60], text, str(params.get("folder") or ""))
    if action == "append":
        return _mac(mac.notes_append, title, text) if title and text else _err("need the note's title and the text")
    if action == "read":
        return _mac(mac.notes_read, title) if title else _err("which note?")
    if action == "find":
        return _mac(mac.notes_find, str(params.get("query") or title), int(params.get("limit") or 10))
    return _err("action must be create, append, read or find")


def obsidian(params, **_):
    action = str(params.get("action", ""))
    if action == "vaults":
        return _ok(vaults=mac.vaults())
    try:
        vault = mac.resolve_vault(params.get("vault"))
    except mac.MacError as e:
        return _err(str(e))
    note, text = params.get("note") or None, str(params.get("text") or "")
    if action in ("append", "create"):
        return _mac(mac.obsidian_write, vault, note, text, action) if text else _err("nothing to write")
    if action == "read":
        return _mac(mac.obsidian_read, vault, note)
    if action == "search":
        return _mac(mac.obsidian_search, vault, str(params.get("query") or ""))
    if action == "recent":
        return _mac(mac.obsidian_recent, vault, int(params.get("limit") or 10))
    if action == "open":
        return _mac(mac.obsidian_open, vault, note)
    return _err("action must be append, create, read, search, recent, open or vaults")


def _slack_configured() -> bool:
    return slack_api._token() is not None


# --- schemas ------------------------------------------------------------------------------------

def _schema(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}


_SESSION = {"type": "string", "description": "Which session: its name (master_aws), a project or topic word "
                                             "(aws, trainium, sglang), or a session-id prefix."}

TOOLS = [
    (claude_sessions, _schema(
        "claude_sessions",
        "List Aman's Claude Code master sessions (one per project; each coordinates worker sessions) with "
        "project, topic, his last prompt, how recently each was active, and what each is doing now. "
        "Call this first when he refers to a session loosely.",
        {"include_workers": {"type": "boolean", "description": "Also list worker and untitled sessions."}})),
    (claude_status, _schema(
        "claude_status",
        "Read where a Claude Code session is at: its recent turns (Aman's prompts, worker reports, Claude's "
        "replies, one line per tool call), current activity, and what Friday already told him about it. "
        "Use this to answer 'where are we at', 'is it done', 'what is it doing'. Free and instant.",
        {"session": _SESSION,
         "depth": {"type": "integer", "description": "How many of Aman's own prompts to reach back over (default 3)."}},
        ["session"])),
    (claude_ask, _schema(
        "claude_ask",
        "Ask the session itself a question; it answers with its full context, and the live session is not "
        "touched. Slow (30 s to a few minutes) and costs a model call over the session's whole context, so "
        "prefer claude_status unless Aman asks to check with the session or the transcript can't answer.",
        {"session": _SESSION, "question": {"type": "string"}}, ["session", "question"])),
    (claude_send, _schema(
        "claude_send",
        "Send the next prompt into a Claude Code session in VS Code, exactly as if Aman typed it: opens that "
        "session's tab and submits. Write the prompt the way Aman would, complete and specific. Only send "
        "what he asked for.",
        {"session": _SESSION, "prompt": {"type": "string"},
         "submit": {"type": "boolean", "description": "false leaves it typed but unsent (default true)."}},
        ["session", "prompt"])),
    (claude_dispatch, _schema(
        "claude_dispatch",
        "Start a NEW Claude Code session in VS Code: opens (or focuses) the folder's window, opens a new Claude "
        "Code tab with the first prompt, and sends it. folder: a folder name under ~/code ('friday', 'aws', "
        "'kitten tts') or a path. name: what to call it; use master_<project> (e.g. master_kitten) if Aman wants "
        "it watched like his other masters, else a short name. Afterwards, talk to it with claude_send.",
        {"folder": {"type": "string"}, "prompt": {"type": "string", "description": "The first prompt, complete and specific."},
         "name": {"type": "string"}, "submit": {"type": "boolean", "description": "false leaves the prompt typed, unsent."}},
        ["folder", "prompt"])),
    (personal_notes, _schema(
        "personal_notes",
        "Aman's own long-term notes about himself, kept by his personal Claude sessions in ~/code/personal. "
        f"Areas: {_area_names()}. Read-only. Read the area a conversation needs (a family date: personal; "
        "money: finances), or search with query. Don't pull relationship or finance notes into work "
        "conversations unless he brings them up.",
        {"area": {"type": "string"}, "query": {"type": "string"}})),
    (health_recent, _schema(
        "health_recent",
        "Aman's recent health summaries from his Apple Watch (and Huawei band, through Apple Health), sent daily by "
        "an iPhone Shortcut: sleep, steps, resting heart rate, HRV, active energy. For 'how did I sleep', 'am I "
        "moving enough'. Private: don't bring it up in front of others or in work threads unless he asks.",
        {"days": {"type": "integer", "description": "How many days back (default 7)."}})),
    (friday_alerts, _schema(
        "friday_alerts",
        "What Friday recently announced or held about the Claude Code sessions, plus whether Aman is at the "
        "desk. Use for 'what did I miss'.",
        {"limit": {"type": "integer", "description": "How many recent alerts (default 10)."}})),
    (friday_hush, _schema("friday_hush", "Stop speaking now and drop anything queued to be said.")),
]

SIGNAL_TOOLS = [
    (signal_send, _schema(
        "signal_send",
        "Send a Signal message as Aman to a person or group, by name (a contact, a group, 'me' for his Note to "
        "Self). Trusted people (auto_send in ~/.friday/contacts.json) are sent to directly. For anyone else the first call only returns a "
        "preview: read it back, and send with confirmed=true after Aman says yes. For a person with a style "
        "guide, call contact_style first and write the message the way Aman would.",
        {"to": {"type": "string"}, "text": {"type": "string"},
         "confirmed": {"type": "boolean", "description": "Only after Aman approved this exact text."}},
        ["to", "text"])),
    (signal_recent, _schema(
        "signal_recent",
        "The latest messages in Aman's Signal chat with someone (both sides, oldest first). Use before replying "
        "to or continuing a conversation, and for 'what did she say'.",
        {"name": {"type": "string"}, "count": {"type": "integer", "description": "How many (default 30)."}},
        ["name"])),
    (contact_style, _schema(
        "contact_style",
        "How Aman writes to a specific person (tone, language mix, length, pet names, examples). Read it before "
        "drafting any message to them, and match it.",
        {"name": {"type": "string"}}, ["name"])),
]

SLACK_TOOLS = [
    (slack_catchup, _schema(
        "slack_catchup",
        "Aman's Slack (work), read-only: messages that mention him, unread DMs, and the busiest channels he's "
        "in over the last N hours, with the recent messages. Use for 'what's going on in Slack', 'any pings', "
        "morning briefings. Summarize; don't read it all out.",
        {"hours": {"type": "number", "description": "How far back (default 12)."}})),
    (slack_read, _schema(
        "slack_read", "Recent messages in one Slack channel Aman is in (read-only).",
        {"channel": {"type": "string", "description": "Channel name, with or without #."},
         "hours": {"type": "number", "description": "How far back (default 24)."}}, ["channel"])),
    (slack_search, _schema(
        "slack_search", "Search Aman's Slack (read-only), with Slack search syntax (from:, in:, after:).",
        {"query": {"type": "string"}, "count": {"type": "integer"}}, ["query"])),
]


_TAB = {"type": "integer", "description": "A tab id from brave_open or brave_tabs."}
_READ_OPTS = {
    "scroll": {"type": "integer", "description": "Scroll down this many screens first, collecting what loads "
                                                 "(timelines, long lists). 0-15, default 0. Friday's own tabs only."},
    "links": {"type": "boolean", "description": "Also return the page's links (text + URL), to follow one."},
    "selector": {"type": "string", "description": "CSS selector to read instead of the main content."},
    "max_chars": {"type": "integer", "description": "Text cap (default 12000)."},
}

BRAVE_TOOLS = [
    (brave_open, _schema(
        "brave_open",
        "Open a page in Aman's own Brave, signed in as him, and read it. It opens as a new tab inside the purple "
        "'Friday' tab group (the tabs he shared with you), so he sees what you opened. url: a page; search: words "
        "to look up on Google (instead of url). show: true brings the tab to the front, for when he asked to see "
        "or open something; leave it false when you're just looking something up. Use it for anything that needs "
        "his accounts or a live site: X trending (https://x.com/explore/tabs/trending), his X timeline "
        "(https://x.com/home), X search (https://x.com/search?q=QUERY&f=live), his finance dashboards, Google "
        "Flights (https://www.google.com/travel/flights?q=Flights+to+BLR+from+DEL+on+2026-11-20). Pass tab_id to "
        "reuse one of your tabs.",
        {"url": {"type": "string"}, "search": {"type": "string", "description": "A Google search, instead of url."},
         "show": {"type": "boolean", "description": "Bring the new tab to the front (he asked to see it)."},
         "tab_id": _TAB, **_READ_OPTS})),
    (brave_read, _schema(
        "brave_read",
        "Read a tab in the Friday group (the only tabs you can see; listed at the top of his message). Without "
        "tab_id: the tab he pinned you to, which is 'this page'. Use it for 'summarize this', 'explain this part'. "
        "If he hasn't shared a tab, it fails: ask him to pin you to it (toolbar icon or Option-Shift-F). His tabs "
        "are read-only (no scrolling); yours can scroll.",
        {"tab_id": _TAB, **_READ_OPTS})),
    (brave_focus, _schema(
        "brave_focus",
        "The Friday tab group in his Brave: what he's shared with you. op 'set' pins you to the tab he's looking "
        "at (or tab_id): it joins the group and becomes 'this page' ('look at this tab', 'pin this', 'this is "
        "what I'm working on'). 'clear' stops sharing (ungroups his tabs, closes yours he never opened). 'get' "
        "lists the group.",
        {"op": {"type": "string", "enum": ["get", "set", "clear"]}, "tab_id": _TAB})),
    (brave_tabs, _schema(
        "brave_tabs", "List the tabs in the Friday group: the only tabs you can see (his shared tabs and the ones "
                      "you opened). His other tabs are invisible to you by design.")),
    (brave_click, _schema(
        "brave_click",
        "Click a link, tab or 'Show more' in one of Friday's own Brave tabs, by its visible text or a CSS "
        "selector, then read the result. For navigating and revealing content only: it refuses to post, reply, "
        "like, follow, send, buy, sell, pay or confirm anything.",
        {"tab_id": _TAB, "text": {"type": "string"}, "selector": {"type": "string"},
         "scroll": _READ_OPTS["scroll"], "links": _READ_OPTS["links"]}, ["tab_id"])),
    (brave_close, _schema(
        "brave_close", "Close one of Friday's Brave tabs (tab_id), or all of them. Never touches Aman's tabs.",
        {"tab_id": _TAB})),
]


MAC_TOOLS = [
    (mac_open, _schema(
        "mac_open",
        "Open something on Aman's Mac: an app by name ('Spotify', 'Obsidian', 'VS Code', 'WhatsApp'), a URL, or "
        "a file or folder path. Opening an app just launches or focuses it.",
        {"target": {"type": "string"}}, ["target"])),
    (mac_quit, _schema("mac_quit", "Quit an app on Aman's Mac by name.", {"app": {"type": "string"}}, ["app"])),
    (mac_volume, _schema(
        "mac_volume", "Read or set the Mac's output volume (0-100) or mute. No arguments: report it.",
        {"level": {"type": "integer"}, "mute": {"type": "boolean"}})),
    (spotify, _schema(
        "spotify",
        "Control the Spotify app on Aman's Mac. action: now (what's playing), play, pause, toggle, next, previous, "
        "volume (Spotify's own, with level), shuffle_on, shuffle_off. To play something specific: action play "
        "with query ('Blinding Lights The Weeknd', 'lofi beats', 'Arijit Singh') and kind (track by default; "
        "album, playlist or artist), or with a Spotify link. play with no query resumes.",
        {"action": {"type": "string", "enum": ["now", "play", "pause", "toggle", "next", "previous", "volume",
                                               "shuffle_on", "shuffle_off"]},
         "query": {"type": "string"},
         "kind": {"type": "string", "enum": ["track", "album", "playlist", "artist"]},
         "link": {"type": "string", "description": "An open.spotify.com link or spotify: URI."},
         "level": {"type": "integer"}}, ["action"])),
    (apple_notes, _schema(
        "apple_notes",
        "Aman's Apple Notes (syncs to his iPhone). action: create (title + text, optional folder), append (text "
        "to the note with that title), read (by title), find (query in titles and text).",
        {"action": {"type": "string", "enum": ["create", "append", "read", "find"]},
         "title": {"type": "string"}, "text": {"type": "string"}, "query": {"type": "string"},
         "folder": {"type": "string"}, "limit": {"type": "integer"}}, ["action"])),
    (obsidian, _schema(
        "obsidian",
        f"Aman's Obsidian vaults (Markdown notes; works with Obsidian closed). Vaults: {_vault_names()}. "
        "action: append (add text to a note; no note = today's daily note; a "
        "missing note is created), create (a new note; fails if it exists), read (no note = today's daily "
        "note), search (query), recent (recently edited notes), open (show the note in the Obsidian app), "
        "vaults. note is a name ('Friday ideas') or a path in the vault ('projects/RSI/Intro'). Write Markdown.",
        {"action": {"type": "string", "enum": ["append", "create", "read", "search", "recent", "open", "vaults"]},
         "note": {"type": "string"}, "text": {"type": "string"}, "query": {"type": "string"},
         "vault": {"type": "string"}, "limit": {"type": "integer"}}, ["action"])),
]


# --- spoken replies -----------------------------------------------------------------------------

def _make_speak_hook(ctx):
    def speak_reply(session_id="", assistant_response="", platform="", **_):
        surface = (ctx.session_surface() or platform or "").lower()
        if surface not in _LOCAL_SURFACES or not assistant_response or _MUTE_FLAG.exists():
            return
        _daemon("/speak", {"text": assistant_response, "source": f"reply:{session_id}"}, timeout=2)
    return speak_reply


def _speak_command(raw: str = "", **_):
    arg = (raw or "").strip().lower()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if arg in ("off", "mute", "quiet"):
        _MUTE_FLAG.touch()
        return "Replies are muted; alerts still speak."
    if arg in ("on", "unmute"):
        _MUTE_FLAG.unlink(missing_ok=True)
        return "Replies will be spoken."
    return "Replies are " + ("muted." if _MUTE_FLAG.exists() else "spoken.") + " Use /speak on|off."


# --- step labels for FridayEars' island ("Checking master_aws…") -----------------------------------

def _q(v, limit: int = 40) -> str:
    v = " ".join(str(v or "").split())
    return v if len(v) <= limit else v[: limit - 1] + "…"


def _host(url) -> str:
    from urllib.parse import urlparse
    u = str(url or "")
    host = urlparse(u if "://" in u else "https://" + u).netloc
    return host[4:] if host.startswith("www.") else host or "a page"


def _step_label(name: str, p: dict) -> str | None:
    """What the island says while one of Friday's tools runs. Signal labels name no one: the island is
    on screen where others can see it."""
    a = str(p.get("action") or "")
    labels = {
        "claude_sessions": lambda: "Looking over your Claude sessions",
        "claude_status": lambda: f"Checking {_q(p.get('session'))}",
        "claude_ask": lambda: f"Asking {_q(p.get('session'))} directly",
        "claude_send": lambda: f"Sending a prompt to {_q(p.get('session'))}",
        "claude_dispatch": lambda: f"Starting a Claude Code session in {_q(p.get('folder'))}",
        "friday_alerts": lambda: "Checking what you missed",
        "personal_notes": lambda: "Checking your notes",
        "health_recent": lambda: "Checking your health data",
        "signal_send": lambda: "Sending a Signal message",
        "signal_recent": lambda: "Reading Signal",
        "contact_style": lambda: "Getting your texting style",
        "slack_catchup": lambda: "Catching up on Slack",
        "slack_read": lambda: f"Reading #{_q(str(p.get('channel') or '').lstrip('#'))} on Slack",
        "slack_search": lambda: f"Searching Slack for “{_q(p.get('query'))}”",
        "brave_open": lambda: (f"Searching Google for “{_q(p.get('search'))}”" if p.get("search")
                               else f"Opening {_host(p.get('url'))} in Brave"),
        "brave_read": lambda: "Reading the page" if p.get("tab_id") is not None else "Reading your Brave tab",
        "brave_tabs": lambda: "Looking at the Friday tab group",
        "brave_focus": lambda: {"set": "Pinning Friday to your tab", "clear": "Stopping tab sharing"}.get(
            str(p.get("op") or "get"), "Checking what you've shared"),
        "brave_click": lambda: f"Clicking “{_q(p.get('text') or p.get('selector'), 30)}”",
        "brave_close": lambda: "Closing Friday's tabs",
        "mac_open": lambda: f"Opening {_q(p.get('target'))}",
        "mac_quit": lambda: f"Quitting {_q(p.get('app'))}",
        "mac_volume": lambda: "Setting the volume" if p.get("level") is not None or p.get("mute") is not None
                              else "Checking the volume",
        "spotify": lambda: (f"Finding “{_q(p.get('query'))}” on Spotify" if a == "play" and p.get("query")
                            else {"now": "Checking what's playing", "play": "Playing Spotify",
                                  "pause": "Pausing Spotify", "next": "Skipping the track",
                                  "previous": "Going back a track"}.get(a, "Controlling Spotify")),
        "apple_notes": lambda: {"create": "Writing a note in Notes",
                                "append": f"Adding to “{_q(p.get('title'))}” in Notes",
                                "read": f"Opening “{_q(p.get('title'))}” in Notes",
                                "find": f"Searching Notes for “{_q(p.get('query') or p.get('title'))}”"}.get(a, "Using Notes"),
        "obsidian": lambda: {"append": f"Writing to {_q(p.get('note')) or 'today’s note'} in Obsidian",
                             "create": f"Creating {_q(p.get('note'))} in Obsidian",
                             "read": f"Reading {_q(p.get('note')) or 'today’s note'}",
                             "search": f"Searching Obsidian for “{_q(p.get('query'))}”",
                             "recent": "Looking at recent Obsidian notes",
                             "open": f"Opening {_q(p.get('note')) or 'Obsidian'}"}.get(a, "Using Obsidian"),
    }
    fn = labels.get(name)
    return fn() if fn else None


def _labelled(name: str, handler):
    def run(params, **kw):
        label = _step_label(name, params or {})
        if label and _is_voice(kw.get("session_id")):
            threading.Thread(target=_daemon, args=("/activity", {"tool": name, "label": label}, 0.5),
                             daemon=True).start()
        return handler(params, **kw)
    return run


def register(ctx):
    for handler, schema in TOOLS:
        ctx.register_tool(name=schema["name"], toolset="friday", schema=schema,
                          handler=_labelled(schema["name"], handler))
    for handler, schema in SIGNAL_TOOLS:
        ctx.register_tool(name=schema["name"], toolset="friday", schema=schema,
                          handler=_labelled(schema["name"], handler))
    for handler, schema in SLACK_TOOLS:   # only offered once SLACK_USER_TOKEN is in ~/.friday/.env
        ctx.register_tool(name=schema["name"], toolset="friday", schema=schema, check_fn=_slack_configured,
                          handler=_labelled(schema["name"], handler))
    for handler, schema in MAC_TOOLS:
        ctx.register_tool(name=schema["name"], toolset="friday", schema=schema,
                          handler=_labelled(schema["name"], handler))
    for handler, schema in BRAVE_TOOLS:
        ctx.register_tool(name=schema["name"], toolset="friday", schema=schema,
                          handler=_labelled(schema["name"], handler))
    ctx.register_hook("post_llm_call", _make_speak_hook(ctx))
    ctx.register_command("speak", _speak_command, "Turn spoken replies on or off: /speak on|off")
    ctx.register_command("hush", lambda raw="", **_: json.loads(friday_hush({})).get("error") or "Quiet.",
                         "Stop Friday speaking right now")
