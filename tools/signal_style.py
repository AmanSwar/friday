"""Learn how Aman writes to one Signal contact, from Signal Desktop's local history.

    signal_style.py find  <name>    which conversation matches, and message counts (no message text printed)
    signal_style.py build <name>    style guide → ~/.friday/memories/contacts/<name>.md (mode 600)

Signal Desktop's database is SQLCipher-encrypted; its key is wrapped with the "Signal Safe Storage"
secret in the macOS Keychain (macOS asks Aman to allow that). Only the matching conversation is
queried. Messages are held in memory, sent once to Claude to distil the style guide, and not written
to disk; only the guide is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SIGNAL = Path.home() / "Library/Application Support/Signal"
OUT_DIR = Path.home() / ".friday/memories/contacts"
CONTACTS = Path.home() / ".friday/contacts.json"


def db_key() -> str:
    secret = subprocess.run(["security", "find-generic-password", "-s", "Signal Safe Storage", "-w"],
                            capture_output=True, text=True, check=True).stdout.strip()
    enc = bytes.fromhex(json.loads((SIGNAL / "config.json").read_text())["encryptedKey"])
    if enc[:3] != b"v10":
        raise SystemExit("unexpected key format")
    aes = hashlib.pbkdf2_hmac("sha1", secret.encode(), b"saltysalt", 1003, 16)   # Chromium safeStorage
    out = subprocess.run(["openssl", "enc", "-aes-128-cbc", "-d", "-K", aes.hex(), "-iv", "20" * 16],
                         input=enc[3:], capture_output=True, check=True).stdout
    return out.decode().strip()


def query(key: str, sql: str) -> list[dict]:
    script = f"PRAGMA key = \"x'{key}'\";\n.mode json\n{sql};\n"
    proc = subprocess.run(["sqlcipher", "-readonly", str(SIGNAL / "sql/db.sqlite")], input=script,
                          capture_output=True, text=True)
    if proc.returncode != 0 or "Error" in proc.stderr:
        raise SystemExit(f"sqlcipher: {proc.stderr.strip()[:300]}")
    text = proc.stdout.strip()
    start = text.find("[")
    return json.loads(text[start:]) if start >= 0 else []


def columns(key: str, table: str) -> set[str]:
    return {r["name"] for r in query(key, f"SELECT name FROM pragma_table_info('{table}')")}


def find(key: str, name: str) -> list[dict]:
    cols = columns(key, "conversations")
    fields = [c for c in ("name", "profileName", "profileFullName", "systemGivenName", "e164") if c in cols]
    like = " OR ".join(f"lower(coalesce({c},'')) LIKE '%{name.lower()}%'" for c in fields)
    rows = query(key, f"SELECT id, type, {', '.join(fields)}, "
                      f"(SELECT count(*) FROM messages m WHERE m.conversationId = c.id) AS n "
                      f"FROM conversations c WHERE {like} ORDER BY n DESC")
    return rows


def messages(key: str, conversation_id: str, limit: int = 4000) -> list[dict]:
    rows = query(key, f"SELECT type, body, sent_at FROM messages WHERE conversationId = '{conversation_id}' "
                      f"AND type IN ('incoming','outgoing') AND body IS NOT NULL AND body != '' "
                      f"ORDER BY sent_at DESC LIMIT {limit}")
    rows.reverse()
    return rows


GUIDE_PROMPT = """You're helping a personal assistant write Signal messages *as Aman*, to {who}, someone close to him.
Below is their recent chat ("ME:" is Aman, "{WHO}:" is {who}). Study only how Aman writes to her, and produce a
style guide another model can follow to draft messages that she couldn't tell apart from his.

Cover, concretely and with short quoted examples of his own phrasing:
- language mix (English / Hindi / Hinglish, transliteration habits), spelling and slang, capitalisation, punctuation
- typical message length and whether he splits a thought across several messages
- pet names, greetings, sign-offs, recurring phrases and in-jokes, emoji and how often
- tone in different moods: casual, affectionate, apologetic, busy, teasing, supportive, when she's upset
- how he replies to questions, plans, and "where are you" type messages
- things he never does (e.g. formal phrasing, long paragraphs)
Then give 12 short example pairs, "{WHO}: …" → "ME: …", picked to cover the range above.

Leave out anything sensitive that isn't about style: addresses, phone numbers, money details, health, passwords,
third parties' private matters. Keep the whole guide under 700 words. Output markdown only.

CHAT:
{chat}"""


def build(key: str, name: str) -> None:
    import anthropic

    rows = find(key, name)
    if not rows:
        raise SystemExit(f"no conversation matches {name!r}")
    conv = rows[0]
    who = conv.get("profileFullName") or conv.get("name") or conv.get("profileName") or name
    msgs = messages(key, conv["id"])
    lines, total = [], 0
    for m in reversed(msgs):                       # newest first until ~60k characters, then restore order
        line = f"{'ME' if m['type'] == 'outgoing' else who.split()[0].upper()}: {' '.join(m['body'].split())[:400]}"
        total += len(line)
        if total > 60000:
            break
        lines.append(line)
    lines.reverse()
    for k, v in (l.split("=", 1) for l in (Path.home() / ".friday/.env").read_text().splitlines() if "=" in l and not l.startswith("#")):
        os.environ.setdefault(k.strip(), v.strip())
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-5-5", max_tokens=8000,
        messages=[{"role": "user", "content": GUIDE_PROMPT.format(who=who.split()[0], WHO=who.split()[0].upper(),
                                                                   chat="\n".join(lines))}])
    if resp.stop_reason == "refusal":
        raise SystemExit("the model declined to write the guide")
    guide = next(b.text for b in resp.content if b.type == "text")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name.lower()}.md"
    header = (f"# How Aman writes to {who}\n\n_Distilled {time.strftime('%Y-%m-%d')} from {len(lines)} recent "
              f"Signal messages ({sum(l.startswith('ME:') for l in lines)} his). Raw messages were not kept._\n\n")
    path.write_text(header + guide)
    os.chmod(path, 0o600)
    contacts = json.loads(CONTACTS.read_text()) if CONTACTS.exists() else {}
    contacts[name.lower()] = {**contacts.get(name.lower(), {}), "display": who, "signal": conv.get("e164"),
                              "style_guide": str(path), "auto_send": True}
    CONTACTS.write_text(json.dumps(contacts, indent=2))
    os.chmod(CONTACTS, 0o600)
    print(f"wrote {path} ({len(guide)} chars) from {len(lines)} messages; contacts.json updated")


if __name__ == "__main__":
    cmd, name = sys.argv[1], sys.argv[2]
    key = db_key()
    if cmd == "find":
        for r in find(key, name):
            print({k: (v if k != "e164" else (v[:3] + "•••" + v[-3:] if v else v)) for k, v in r.items()})
    elif cmd == "build":
        build(key, name)
