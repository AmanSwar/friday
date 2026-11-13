"""Friday's hands on the Mac: apps, volume, Spotify (desktop app), Apple Notes, Obsidian.

Spotify and Notes are driven with AppleScript. The first use of each asks Aman once to let FridayEars
(the app Friday runs under) control it. Obsidian notes are Markdown files in his vaults, written
directly, so they work with Obsidian closed too."""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Optional


class MacError(Exception):
    pass


def osascript(script: str, *args: str, timeout: float = 20) -> str:
    """Run AppleScript (``on run argv`` receives ``args``); returns stdout or raises MacError."""
    try:
        r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MacError("the app didn't respond in time") from None
    if r.returncode != 0:
        err = r.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            raise MacError("macOS hasn't allowed Friday to control that app yet. A prompt should appear "
                           "(\"FridayEars wants access to control ...\"); click OK, or enable it in System Settings, "
                           "Privacy & Security, Automation, FridayEars.")
        if "-600" in err or "isn't running" in err:
            raise MacError("that app isn't running")
        raise MacError(err.split("execution error: ")[-1][:300] or "AppleScript failed")
    return r.stdout.rstrip("\n")


# --- apps, URLs, files, volume --------------------------------------------------------------------

_APP_DIRS = [Path("/Applications"), Path("/System/Applications"), Path("/System/Applications/Utilities"),
             Path.home() / "Applications", Path("/Applications/Utilities")]


_ALIASES = {"vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "code": "Visual Studio Code",
            "chrome": "Google Chrome", "brave": "Brave Browser", "settings": "System Settings",
            "system preferences": "System Settings", "music": "Music", "apple music": "Music",
            "notes": "Notes", "apple notes": "Notes", "calendar": "Calendar", "mail": "Mail"}


def find_app(name: str) -> Optional[Path]:
    """The .app for a spoken name: exact (case-insensitive) first, then the closest contains-match."""
    want = name.strip().lower().removesuffix(".app")
    want = _ALIASES.get(want, want).lower()
    apps = [p for d in _APP_DIRS if d.is_dir() for p in d.glob("*.app")]
    for p in apps:
        if p.stem.lower() == want:
            return p
    hits = sorted((p for p in apps if want in p.stem.lower()), key=lambda p: len(p.stem))
    if hits:
        return hits[0]
    squashed = want.replace(" ", "")
    hits = sorted((p for p in apps if squashed in p.stem.lower().replace(" ", "")), key=lambda p: len(p.stem))
    return hits[0] if hits else None


def open_target(target: str) -> dict:
    """Open an app by name, a URL (any scheme: https, obsidian://, spotify:), or a file/folder path."""
    target = target.strip()
    if not target:
        raise MacError("nothing to open")
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) and not target.startswith("/"):
        cmd, what = ["open", target], {"url": target}
    elif target.startswith(("/", "~")):
        path = Path(target).expanduser()
        if not path.exists():
            raise MacError(f"{path} doesn't exist")
        cmd, what = ["open", str(path)], {"path": str(path)}
    else:
        app = find_app(target)
        if app is None:
            raise MacError(f"no app called {target!r} on this Mac")
        cmd, what = ["open", "-a", str(app)], {"app": app.stem}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise MacError(r.stderr.strip()[:300] or "open failed")
    return {"opened": True, **what}


def quit_app(name: str) -> dict:
    app = find_app(name)
    if app is None:
        raise MacError(f"no app called {name!r} on this Mac")
    if osascript('on run argv\nreturn application (item 1 of argv) is running\nend run', app.stem) != "true":
        return {"app": app.stem, "was_running": False}
    osascript('on run argv\ntell application (item 1 of argv) to quit\nend run', app.stem)
    return {"app": app.stem, "quit": True}


def running_apps() -> list[str]:
    out = osascript('tell application "System Events" to get name of every application process whose background only is false')
    return [a.strip() for a in out.split(",") if a.strip()]


def volume(level: Optional[int] = None, mute: Optional[bool] = None) -> dict:
    if mute is not None:
        osascript(f"set volume {'with' if mute else 'without'} output muted")
    if level is not None:
        osascript(f"set volume output volume {max(0, min(100, int(level)))}")
    out = osascript("set s to get volume settings\nreturn (output volume of s as string) & \",\" & (output muted of s as string)")
    vol, muted = out.split(",")
    return {"volume": int(vol) if vol.isdigit() else vol, "muted": muted == "true"}


# --- Spotify (the desktop app) --------------------------------------------------------------------

SPOTIFY_LINK = re.compile(r"https://open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]{22})")


def spotify_uri(link: str) -> Optional[str]:
    if re.fullmatch(r"spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]{22}", link.strip()):
        return link.strip()
    m = SPOTIFY_LINK.search(link)
    return f"spotify:{m.group(1)}:{m.group(2)}" if m else None


def _spotify_running() -> bool:
    return osascript('return application "Spotify" is running') == "true"


def _ensure_spotify() -> None:
    if _spotify_running():
        return
    subprocess.run(["open", "-g", "-a", "Spotify"], capture_output=True, timeout=15)   # -g: don't take focus
    for _ in range(30):
        time.sleep(0.5)
        if _spotify_running():
            time.sleep(2)   # the app answers AppleScript a moment after it launches
            return
    raise MacError("Spotify didn't start")


def spotify_now() -> dict:
    if not _spotify_running():
        return {"running": False}
    out = osascript('''tell application "Spotify"
  set s to player state as string
  if s is "stopped" then return s
  set t to current track
  set pos to player position          -- fetch first: "round (player position)" fails with -1700
  set dur to duration of t
  return s & linefeed & (name of t) & linefeed & (artist of t) & linefeed & (album of t) & linefeed & ¬
    (sound volume as string) & linefeed & ((round pos) as string) & linefeed & ((round (dur / 1000)) as string) & ¬
    linefeed & (shuffling as string)
end tell''')
    lines = out.split("\n")
    if lines[0] == "stopped" or len(lines) < 8:
        return {"running": True, "state": lines[0]}
    state, name, artist, album, vol, pos, dur, shuffle = lines[:8]
    return {"running": True, "state": state, "track": name, "artist": artist, "album": album,
            "spotify_volume": int(vol) if vol.isdigit() else vol, "position_s": pos, "duration_s": dur,
            "shuffle": shuffle == "true"}


def spotify(action: str, uri: Optional[str] = None, level: Optional[int] = None) -> dict:
    commands = {"play": "play", "resume": "play", "pause": "pause", "toggle": "playpause",
                "next": "next track", "skip": "next track", "previous": "previous track", "back": "previous track"}
    if action == "now":
        return spotify_now()
    _ensure_spotify()
    if action == "play_uri":
        if not uri:
            raise MacError("no Spotify link to play")
        osascript('on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run', uri)
    elif action in commands:
        osascript(f'tell application "Spotify" to {commands[action]}')
    elif action == "volume":
        if level is None:
            raise MacError("give a volume from 0 to 100")
        osascript(f'tell application "Spotify" to set sound volume to {max(0, min(100, int(level)))}')
    elif action in ("shuffle_on", "shuffle_off"):
        osascript(f'tell application "Spotify" to set shuffling to {"true" if action == "shuffle_on" else "false"}')
    else:
        raise MacError(f"unknown Spotify action {action}")
    time.sleep(0.6)   # let the player state catch up before reporting it
    return spotify_now()


# --- Apple Notes ----------------------------------------------------------------------------------

def _note_html(title: Optional[str], text: str) -> str:
    lines = [f"<div><b>{html.escape(title)}</b></div>"] if title else []
    lines += [f"<div>{html.escape(line) or '<br>'}</div>" for line in text.split("\n")]
    return "".join(lines)


def notes_create(title: str, text: str, folder: str = "") -> dict:
    nid = osascript('''on run argv
  set t to item 1 of argv
  set b to item 2 of argv
  set f to item 3 of argv
  tell application "Notes"
    if f is "" then
      set n to make new note with properties {name:t, body:b}
    else
      if not (exists folder f) then make new folder with properties {name:f}
      set n to make new note at folder f with properties {name:t, body:b}
    end if
    return id of n
  end tell
end run''', title, _note_html(title, text), folder)
    return {"created": title, "folder": folder or "Notes", "id": nid}


_FIND_NOTE = '''on findNote(t)
  tell application "Notes"
    set hits to (notes whose name is t)
    if (count of hits) is 0 then set hits to (notes whose name contains t)
    if (count of hits) is 0 then error "no note called " & t
    set best to item 1 of hits
    repeat with n in hits
      if modification date of n > modification date of best then set best to n
    end repeat
    return best
  end tell
end findNote
'''


def notes_append(title: str, text: str) -> dict:
    name = osascript(_FIND_NOTE + '''on run argv
  set n to my findNote(item 1 of argv)
  tell application "Notes"
    set body of n to (body of n) & (item 2 of argv)
    return name of n
  end tell
end run''', title, _note_html(None, text))
    return {"appended_to": name}


def notes_read(title: str, max_chars: int = 8000) -> dict:
    out = osascript(_FIND_NOTE + '''on run argv
  set n to my findNote(item 1 of argv)
  tell application "Notes" to return (name of n) & linefeed & ((modification date of n) as string) & linefeed & (plaintext of n)
end run''', title)
    name, modified, body = (out.split("\n", 2) + ["", ""])[:3]
    return {"title": name, "modified": modified, "text": body[:max_chars], "truncated": len(body) > max_chars}


def notes_find(query: str, limit: int = 10) -> dict:
    out = osascript('''on run argv
  set q to item 1 of argv
  set lim to (item 2 of argv) as integer
  set out to ""
  tell application "Notes"
    set hits to (notes whose name contains q)
    if (count of hits) < lim then set hits to hits & (notes whose plaintext contains q)
    set seen to {}
    repeat with n in hits
      if (count of seen) ≥ lim then exit repeat
      set i to id of n
      if seen does not contain i then
        set end of seen to i
        set out to out & (name of n) & tab & ((modification date of n) as string) & tab & (name of container of n) & linefeed
      end if
    end repeat
  end tell
  return out
end run''', query, str(limit), timeout=40)
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    return {"notes": [{"title": r[0], "modified": r[1] if len(r) > 1 else "", "folder": r[2] if len(r) > 2 else ""}
                      for r in rows]}


# --- Obsidian -------------------------------------------------------------------------------------

_OBSIDIAN_CONFIG = Path.home() / "Library/Application Support/obsidian/obsidian.json"
_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}


def vaults() -> list[dict]:
    try:
        data = json.loads(_OBSIDIAN_CONFIG.read_text())
    except (OSError, ValueError):
        return []
    out = [{"name": Path(v["path"]).name, "path": v["path"], "open": bool(v.get("open"))}
           for v in (data.get("vaults") or {}).values() if v.get("path") and Path(v["path"]).is_dir()]
    return sorted(out, key=lambda v: not v["open"])


def resolve_vault(name: Optional[str] = None, default: Optional[str] = None) -> dict:
    vs = vaults()
    if not vs:
        raise MacError("no Obsidian vaults found")
    for want in (name, default):
        if want:
            w = want.strip().lower()
            hit = next((v for v in vs if v["name"].lower() == w), None) or \
                next((v for v in vs if w in v["name"].lower()), None)
            if hit:
                return hit
            if want == name:
                raise MacError(f"no vault called {name!r}; vaults: {', '.join(v['name'] for v in vs)}")
    return vs[0]   # the one open in Obsidian


def _notes_in(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".md"):
                yield Path(dirpath) / f


def _note_path(vault: dict, note: str, must_exist: bool) -> Path:
    root = Path(vault["path"]).resolve()
    rel = note.strip().strip("/")
    if not rel:
        raise MacError("which note?")
    if not rel.endswith(".md"):
        rel += ".md"
    path = (root / rel).resolve()
    if root not in path.parents:
        raise MacError("that path is outside the vault")
    if path.exists() or "/" in rel:
        if must_exist and not path.exists():
            raise MacError(f"no note {rel} in {vault['name']}")
        return path
    stem = Path(rel).stem.lower()   # a bare name: find it anywhere in the vault
    hits = [p for p in _notes_in(root) if p.stem.lower() == stem]
    if hits:
        return max(hits, key=lambda p: p.stat().st_mtime)
    if must_exist:
        raise MacError(f"no note called {note!r} in {vault['name']}")
    return path


def _rel(vault: dict, path: Path) -> str:
    return str(path.resolve().relative_to(Path(vault["path"]).resolve()))


def daily_note_path(vault: dict) -> Path:
    """Today's daily note, following the vault's Daily Notes settings (folder + date format)."""
    root = Path(vault["path"])
    folder, fmt = "", "YYYY-MM-DD"
    try:
        cfg = json.loads((root / ".obsidian" / "daily-notes.json").read_text())
        folder, fmt = cfg.get("folder") or "", cfg.get("format") or fmt
    except (OSError, ValueError):
        pass
    strf = fmt
    for token, code in (("YYYY", "%Y"), ("YY", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
                        ("DD", "%d"), ("dddd", "%A"), ("ddd", "%a")):
        strf = strf.replace(token, code)
    if re.search(r"[A-Za-z]", re.sub(r"%[a-zA-Z]", "", strf)):   # a format this doesn't understand
        strf = "%Y-%m-%d"
    return root / folder / (_dt.date.today().strftime(strf) + ".md")


def obsidian_write(vault: dict, note: Optional[str], text: str, mode: str = "append") -> dict:
    path = daily_note_path(vault) if not note else _note_path(vault, note, must_exist=False)
    if mode == "create" and path.exists():
        raise MacError(f"{_rel(vault, path)} already exists; append to it instead?")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("a", encoding="utf-8") as fh:
        if existed:
            current = path.read_text(encoding="utf-8")
            if current and not current.endswith("\n"):
                fh.write("\n")
        fh.write(text.rstrip("\n") + "\n")
    return {"vault": vault["name"], "note": _rel(vault, path), "created": not existed}


def obsidian_read(vault: dict, note: Optional[str], max_chars: int = 12000) -> dict:
    path = daily_note_path(vault) if not note else _note_path(vault, note, must_exist=True)
    if not path.exists():
        raise MacError("there's no daily note for today yet")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"vault": vault["name"], "note": _rel(vault, path), "text": text[:max_chars], "truncated": len(text) > max_chars}


def obsidian_search(vault: dict, query: str, limit: int = 15) -> dict:
    q = query.strip().lower()
    if not q:
        raise MacError("search for what?")
    hits = []
    for p in _notes_in(Path(vault["path"])):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_name = q in p.stem.lower()
        idx = text.lower().find(q)
        if in_name or idx >= 0:
            snippet = text[max(0, idx - 80): idx + 160].replace("\n", " ").strip() if idx >= 0 else text[:160].replace("\n", " ")
            hits.append((not in_name, -p.stat().st_mtime, {"note": _rel(vault, p), "snippet": snippet}))
    hits.sort(key=lambda h: (h[0], h[1]))
    return {"vault": vault["name"], "matches": [h[2] for h in hits[:limit]], "total": len(hits)}


def obsidian_recent(vault: dict, limit: int = 10) -> dict:
    notes = sorted(_notes_in(Path(vault["path"])), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return {"vault": vault["name"], "recent": [{"note": _rel(vault, p),
                                               "modified": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                                              for p in notes]}


def obsidian_open(vault: dict, note: Optional[str]) -> dict:
    if note:
        path = _note_path(vault, note, must_exist=True)
    else:
        path = daily_note_path(vault)
    query = {"vault": vault["name"]}
    if path.exists():
        query["file"] = _rel(vault, path).removesuffix(".md")
    subprocess.run(["open", "obsidian://open?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)],
                   capture_output=True, timeout=15)
    return {"opened": query.get("file") or vault["name"], "vault": vault["name"]}
