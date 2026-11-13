"""Read Aman's Signal history from Signal Desktop's local database (read-only).

The database is SQLCipher-encrypted; its key is wrapped with the "Signal Safe Storage" secret in the
macOS Keychain (Chromium safeStorage: PBKDF2-SHA1 "saltysalt" x1003 → AES-128-CBC, IV of spaces).
macOS asks Aman to allow the Keychain read. The unwrapped key is kept in memory only.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

SIGNAL = Path.home() / "Library/Application Support/Signal"
_key: Optional[str] = None


class SignalDesktopError(RuntimeError):
    pass


def db_key() -> str:
    global _key
    if _key:
        return _key
    proc = subprocess.run(["security", "find-generic-password", "-s", "Signal Safe Storage", "-w"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SignalDesktopError("couldn't read Signal's key from the Keychain (was the prompt denied?)")
    enc = bytes.fromhex(json.loads((SIGNAL / "config.json").read_text())["encryptedKey"])
    if enc[:3] != b"v10":
        raise SignalDesktopError("unexpected Signal key format")
    aes = hashlib.pbkdf2_hmac("sha1", proc.stdout.strip().encode(), b"saltysalt", 1003, 16)
    out = subprocess.run(["openssl", "enc", "-aes-128-cbc", "-d", "-K", aes.hex(), "-iv", "20" * 16],
                         input=enc[3:], capture_output=True, check=True).stdout
    _key = out.decode().strip()
    return _key


def query(sql: str) -> list[dict]:
    script = f"PRAGMA key = \"x'{db_key()}'\";\n.mode json\n{sql};\n"
    proc = subprocess.run(["/opt/homebrew/bin/sqlcipher", "-readonly", str(SIGNAL / "sql/db.sqlite")],
                          input=script, capture_output=True, text=True)
    if proc.returncode != 0 or "Error" in proc.stderr:
        raise SignalDesktopError(f"sqlcipher: {proc.stderr.strip()[:200]}")
    text = proc.stdout.strip()
    start = text.find("[")
    return json.loads(text[start:]) if start >= 0 else []


def _safe(s: str) -> str:
    return re.sub(r"[^\w\s@.+-]", "", s.lower())


def find_conversation(name: str) -> dict:
    n = _safe(name)
    rows = query("SELECT id, type, name, profileFullName, profileName, "
                 "(SELECT count(*) FROM messages m WHERE m.conversationId = c.id) AS n FROM conversations c "
                 f"WHERE lower(coalesce(name,'')||' '||coalesce(profileFullName,'')||' '||coalesce(profileName,'')) "
                 f"LIKE '%{n}%' ORDER BY n DESC LIMIT 1")
    if not rows:
        raise SignalDesktopError(f"no Signal conversation matches {name!r}")
    return rows[0]


def recent(name: str, count: int = 30) -> dict:
    conv = find_conversation(name)
    who = conv.get("profileFullName") or conv.get("name") or name
    rows = query(f"SELECT type, body, sent_at FROM messages WHERE conversationId = '{conv['id']}' "
                 "AND type IN ('incoming','outgoing') AND body IS NOT NULL AND body != '' "
                 f"ORDER BY sent_at DESC LIMIT {int(count)}")
    msgs = [{"from": "Aman" if r["type"] == "outgoing" else who,
             "when": time.strftime("%a %d %b %H:%M", time.localtime(r["sent_at"] / 1000)),
             "text": r["body"]} for r in reversed(rows)]
    return {"conversation": who, "messages": msgs,
            "note": "From Signal Desktop's copy; very recent messages appear once Signal Desktop has synced."}
