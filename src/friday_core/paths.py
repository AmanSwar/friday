"""Filesystem locations shared by fridayd, the Hermes plugin and the Claude Code hook."""

from __future__ import annotations

import os
from pathlib import Path

FRIDAY_HOME = Path(os.environ.get("FRIDAY_HOME", Path.home() / ".friday"))
STATE_DIR = FRIDAY_HOME / "state"
EVENTS_FILE = STATE_DIR / "claude-events.jsonl"   # appended by bin/friday-cc-hook
ALERTS_FILE = STATE_DIR / "alerts.jsonl"          # what fridayd said (or sent) and why
DAEMON_URL = os.environ.get("FRIDAYD_URL", "http://127.0.0.1:8765")

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CLAUDE_PROJECTS = CLAUDE_HOME / "projects"

# The Friday extension in Aman's Brave connects here (fridayd hosts it) and proves itself with this token.
BRIDGE_PORT = int(os.environ.get("FRIDAY_BRIDGE_PORT", "8766"))
BRIDGE_TOKEN_FILE = STATE_DIR / "brave-bridge-token"


def bridge_token() -> str:
    """The extension's shared secret, created on first use (owner-only)."""
    try:
        return BRIDGE_TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        import secrets
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(BRIDGE_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token)
        return token
