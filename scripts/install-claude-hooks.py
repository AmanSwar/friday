#!/usr/bin/env python3
"""Add Friday's Claude Code hooks to ~/.claude/settings.json (run it yourself; it edits global config).

Adds one async command hook per event, all running bin/friday-cc-hook, which appends the event to
~/.friday/state/claude-events.jsonl and always exits 0. Async means sessions never wait on it.
Plus two that Claude Code waits for:
- PermissionRequest (bin/friday-permission-hook, 120 s): Aman can allow/deny by voice. Unsupervised
  sessions, no answer, or fridayd down: no output, so the normal dialog shows (and a click there wins).
- SessionStart (bin/friday-session-hook, 5 s): registers the session and names sessions Friday dispatched.
Existing settings and hooks are kept; a backup is written next to the file first.

    python3 scripts/install-claude-hooks.py            # install
    python3 scripts/install-claude-hooks.py --remove   # take Friday's hooks out again
"""
import json
import shutil
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
BIN = Path(__file__).resolve().parent.parent / "bin"
HOOK_CMD = str(BIN / "friday-cc-hook")
SYNC = {                         # event -> (command, timeout s): Claude Code waits for these
    "PermissionRequest": (str(BIN / "friday-permission-hook"), 120),
    "SessionStart": (str(BIN / "friday-session-hook"), 5),
}
EVENTS = {                       # event -> matcher (None: every occurrence)
    "Stop": None,
    "StopFailure": None,
    "UserPromptSubmit": None,
    "Notification": "permission_prompt|elicitation_dialog",
    "PreToolUse": "AskUserQuestion",
}


def ours(group: dict) -> bool:
    mine = {HOOK_CMD} | {cmd for cmd, _ in SYNC.values()}
    return any(h.get("command") in mine for h in group.get("hooks", []))


def main() -> None:
    raw = SETTINGS.read_text() if SETTINGS.exists() else "{}\n"
    settings = json.loads(raw)
    hooks = settings.setdefault("hooks", {})
    remove = "--remove" in sys.argv
    for event, matcher in EVENTS.items():
        groups = [g for g in hooks.get(event, []) if not ours(g)]
        if not remove:
            group = {"hooks": [{"type": "command", "command": HOOK_CMD, "async": True, "timeout": 5}]}
            if matcher:
                group = {"matcher": matcher, **group}
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    for event, (cmd, timeout) in SYNC.items():
        groups = [g for g in hooks.get(event, []) if not ours(g)]
        if not remove:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_suffix(".json.friday-backup"))
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(("Removed" if remove else "Installed") + f" Friday hooks in {SETTINGS} (backup: settings.json.friday-backup)")
    print("Running sessions pick it up after /hooks is opened once or the session restarts.")


if __name__ == "__main__":
    main()
