"""Act on Claude Code sessions: ask a master a question, or send it the next prompt."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlparse

from .claude import Session

VSCODE_STORAGE = Path.home() / "Library/Application Support/Code/User/globalStorage/storage.json"
EXTENSION_URI = "vscode://anthropic.claude-code/open"


def _claude_bin() -> Optional[str]:
    return shutil.which("claude") or next(
        (str(p) for p in [Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude")] if p.exists()), None)


def ask(session: Session, question: str, timeout: float = 300) -> dict:
    """Ask the master itself, with its full context, without touching the live session.

    ``--fork-session`` resumes into a new session id and ``--no-session-persistence`` keeps that fork
    off disk, so nothing appears in the session list. Costs one model call over the master's whole
    context (cheap only while its prompt cache is warm)."""
    claude = _claude_bin()
    if not claude:
        return {"ok": False, "error": "claude CLI not found"}
    cwd = next((d for d in (session.project_dir, session.cwd) if d and Path(d).is_dir()), str(Path.home()))
    if not Path(cwd).is_dir():
        cwd = str(Path.home())
    # plan mode: the fork can read and look, never change anything (it runs with his allow rules otherwise:
    # ssh, git commit, ...); a few turns at most, so a fork that thinks it's mid-task can't go off and work.
    argv = [claude, "-p", "--resume", session.session_id, "--fork-session", "--no-session-persistence",
            "--permission-mode", "plan", "--max-turns", "3", "--output-format", "json", question]
    started = time.time()
    try:
        # FRIDAY_INTERNAL: Friday's own headless fork. Claude Code passes the environment to hooks, and Friday's
        # hooks skip it, so its tool permissions, prompts and stops aren't taken for Aman's session's.
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "FRIDAY_INTERNAL": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"no answer within {int(timeout)}s"}
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return {"ok": False, "error": (proc.stderr or proc.stdout)[-800:]}
    return {"ok": not out.get("is_error"), "answer": out.get("result", ""), "seconds": round(time.time() - started, 1),
            "cost_usd": out.get("total_cost_usd")}


# --- sending a prompt into the VS Code extension -------------------------------------------------

def open_windows() -> list[str]:
    """Folder URIs of open VS Code windows, from VS Code's own window state (no Accessibility needed)."""
    try:
        state = json.loads(VSCODE_STORAGE.read_text()).get("windowsState", {})
    except (OSError, ValueError):
        return []
    wins = [state.get("lastActiveWindow")] + state.get("openedWindows", [])
    return list(dict.fromkeys(w["folder"] for w in wins if w and w.get("folder")))


def _uri_path(uri: str) -> str:
    return unquote(urlparse(uri).path).rstrip("/")


def window_for(session: Session) -> Optional[str]:
    """The open window for the session: the one on its launch folder first (a window on a subfolder its shell
    wandered into isn't the session's), else one containing its current folder."""
    uris = open_windows()
    for where in filter(None, [session.project_dir, session.cwd]):
        where = where.rstrip("/")
        matches = [u for u in uris if where == _uri_path(u) or where.startswith(_uri_path(u) + "/")]
        if matches:
            return max(matches, key=lambda u: len(_uri_path(u)))
    return None


def _osascript(script: str, *args: str, timeout: float = 10) -> tuple[int, str]:
    proc = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _press_return_in_vscode(expect: str) -> tuple[bool, str]:
    """Press Return in VS Code, but only if VS Code is frontmost and the focused text box holds the prompt
    (``expect``): the editor and the terminal are text areas too, and Return there would run a half-typed
    command or add a line to a file. Needs Accessibility for the app running Friday."""
    rc, out = _osascript('''on run argv
tell application "System Events"
  set front to name of first process whose frontmost is true
  if front is not "Code" then return "not-front:" & front
  tell process "Code"
    set el to value of attribute "AXFocusedUIElement"
    set r to role of el
    if r is not in {"AXTextArea", "AXTextField"} then return "focus:" & r
    set v to ""
    try
      set v to value of el as text
    end try
    if v does not contain (item 1 of argv) then return "focus:not-the-prompt"
    key code 36
  end tell
end tell
return "sent"
end run''', expect)
    if rc != 0:
        if "not allowed assistive access" in out or "-1719" in out or "-25211" in out:
            return False, "no-accessibility"
        return False, out[-200:]
    return out == "sent", out


def send(session: Session, prompt: str, submit: bool = True) -> dict:
    """Put ``prompt`` into the session's input box in VS Code and (optionally) press Return.

    The extension's own URI handler opens the exact session and fills its input; only the final
    Return is synthesized. If Return can't be pressed safely the prompt is left typed, unsent."""
    window = window_for(session)
    if window is None:
        return {"ok": False, "error": "no open VS Code window for this session",
                "session_folder": session.cwd, "open_windows": open_windows()}
    code = shutil.which("code") or "/opt/homebrew/bin/code"
    subprocess.run([code, "--folder-uri", window], capture_output=True, timeout=15)  # focuses the existing window
    time.sleep(0.6)
    url = f"{EXTENSION_URI}?session={quote(session.session_id)}&prompt={quote(prompt, safe='')}"
    subprocess.run(["open", url], capture_output=True, timeout=10)
    if not submit:
        return {"ok": True, "sent": False, "typed": True, "window": window}
    time.sleep(1.2)
    sent, detail = _press_return_in_vscode(" ".join(prompt.split())[:40])
    result = {"ok": sent, "sent": sent, "typed": True, "window": window, "detail": detail}
    if detail == "no-accessibility":
        result["error"] = ("The prompt is typed into the session but not sent: macOS hasn't given the app running "
                           "Friday Accessibility permission, so it can't press Return.")
    elif not sent:
        result["error"] = f"The prompt is typed but not sent (Return was not pressed: {detail})."
    return result


def open_session(session: Session) -> dict:
    """Bring up a session in VS Code (the panel's masters list): focus its window, or open its folder,
    then let the Claude Code extension show that exact session. Nothing is typed or sent."""
    code = shutil.which("code") or "/opt/homebrew/bin/code"
    window = window_for(session)
    if window:
        subprocess.run([code, "--folder-uri", window], capture_output=True, timeout=15)
        time.sleep(0.6)
    else:
        folder = session.project_dir or session.cwd
        if not folder:
            return {"ok": False, "error": "don't know which folder this session belongs to"}
        subprocess.run([code, folder], capture_output=True, timeout=15)
        time.sleep(2.5)   # a new window needs a moment before the extension takes URIs
    subprocess.run(["open", f"{EXTENSION_URI}?session={quote(session.session_id)}"], capture_output=True, timeout=10)
    return {"ok": True, "window": window or session.project_dir or session.cwd}


# --- dispatch: start a new Claude Code session in a folder ------------------------------------------

CODE_ROOTS = [Path.home() / "code"]
_SKIP = {"node_modules", ".git", ".venv", "venv", "venvs", "__pycache__", "dist", "build", "target", "site-packages"}
_FILLER = {"my", "the", "a", "project", "folder", "repo", "repository", "directory", "dir", "in", "code"}


def _norm(text: str) -> str:
    return "".join(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _FILLER)


def find_folder(spoken: str, depth: int = 3) -> tuple[Optional[str], list[str]]:
    """A folder from speech: a path, or the best-matching directory name under ~/code (3 levels deep).
    Returns (folder or None, other close matches)."""
    s = spoken.strip()
    if s.startswith(("/", "~")):
        p = Path(s).expanduser()
        return (str(p), []) if p.is_dir() else (None, [])
    want = _norm(s)
    if not want:
        return None, []
    scored: list[tuple[int, int, str]] = []
    for root in CODE_ROOTS:
        stack = [(root, 0)]
        while stack:
            d, level = stack.pop()
            try:
                children = [c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name not in _SKIP]
            except OSError:
                continue
            for c in children:
                name = _norm(c.name)
                score = 100 if name == want else 80 if name.startswith(want) or (
                    want.startswith(name) and len(name) >= 0.7 * len(want)) else 60 if len(want) >= 4 and want in name else 0
                if score:
                    scored.append((score, -level, str(c)))
                if level + 1 < depth:
                    stack.append((c, level + 1))
    scored.sort(reverse=True)
    if not scored:
        return None, []
    best = scored[0]
    others = [p for sc, lvl, p in scored[1:6] if sc == best[0] and lvl == best[1]]   # equally good and equally shallow
    return (best[2] if not others else None), ([best[2]] + others if others else [p for _, _, p in scored[1:4]])


def dispatch(folder: str, prompt: str, submit: bool = True) -> dict:
    """Open (or focus) VS Code on ``folder``, start a new Claude Code conversation there with ``prompt``,
    and press Return. The extension's URI only prefills, and opens in the focused window, so the folder's
    window is focused first and given time to load."""
    path = Path(folder).expanduser()
    if not path.is_dir():
        return {"ok": False, "error": f"{folder} isn't a folder"}
    uri = path.resolve().as_uri()
    already = any(_uri_path(u) == str(path.resolve()) for u in open_windows())
    code = shutil.which("code") or "/opt/homebrew/bin/code"
    subprocess.run([code, "--folder-uri", uri], capture_output=True, timeout=15)
    time.sleep(1.5 if already else 6)          # a new window needs the extension loaded before the URI lands
    subprocess.run(["open", f"{EXTENSION_URI}?prompt={quote(prompt, safe='')}"], capture_output=True, timeout=10)
    if not submit:
        return {"ok": True, "sent": False, "typed": True, "folder": str(path), "new_window": not already}
    time.sleep(1.5)
    sent, detail = _press_return_in_vscode(" ".join(prompt.split())[:40])
    result = {"ok": sent, "sent": sent, "typed": True, "folder": str(path), "new_window": not already, "detail": detail}
    if detail == "no-accessibility":
        result["error"] = ("The prompt is typed into a new Claude Code tab but not sent: macOS hasn't given the app "
                           "running Friday Accessibility permission, so it can't press Return.")
    elif not sent:
        result["error"] = f"The prompt is typed in a new tab but not sent (Return was not pressed: {detail})."
    return result
