"""Send Signal messages as Aman through signal-cli (linked device "Friday Mac").

Recipients resolve by name: ~/.friday/contacts.json first (trusted people, with style guides), then
Signal groups and contacts. Contacts marked auto_send (his partner, say) are sent to directly; anyone
else needs Aman's explicit OK (the tool returns a preview first).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

CONTACTS = Path.home() / ".friday/contacts.json"
SIGNAL_CLI = shutil.which("signal-cli") or "/opt/homebrew/bin/signal-cli"
DAEMON = "http://127.0.0.1:8080"   # signal-cli daemon (scripts/signal-daemon.sh, run by FridayEars)


class SignalError(RuntimeError):
    pass


def _cli(*args: str, timeout: float = 60) -> str:
    proc = subprocess.run([SIGNAL_CLI, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SignalError((proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout) else "signal-cli failed")
    return proc.stdout


def rpc(method: str, **params):
    """JSON-RPC to the running signal-cli daemon. Raises OSError if it isn't running."""
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(f"{DAEMON}/api/v1/rpc", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read() or b"{}")
    if "error" in out:
        raise SignalError(out["error"].get("message", "signal-cli error"))
    return out.get("result")


def daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{DAEMON}/api/v1/check", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


_account_cache: Optional[str] = None


def account() -> str:
    global _account_cache
    if _account_cache:
        return _account_cache
    accounts = Path.home() / ".local/share/signal-cli/data/accounts.json"   # readable while the daemon runs
    if accounts.exists():
        for a in json.loads(accounts.read_text()).get("accounts", []):
            if a.get("number"):
                _account_cache = a["number"]
                return _account_cache
    m = re.search(r"\+\d{6,15}", _cli("listAccounts"))
    if not m:
        raise SignalError("no linked Signal account (run scripts/link-signal.sh)")
    _account_cache = m.group(0)
    return _account_cache


def contacts() -> dict:
    return json.loads(CONTACTS.read_text()) if CONTACTS.exists() else {}


def style_guide(name: str) -> Optional[str]:
    c = contacts().get(name.lower())
    if c and c.get("style_guide") and Path(c["style_guide"]).exists():
        return Path(c["style_guide"]).read_text()
    return None


def resolve(name: str, acct: str) -> dict:
    """{'kind': 'number'|'group', 'id': ..., 'display': ..., 'trusted': bool}"""
    want = name.strip().lower()
    for key, c in contacts().items():
        if want in (key, (c.get("display") or "").lower()) or want in (c.get("display") or "").lower():
            if c.get("signal"):
                return {"kind": "number", "id": c["signal"], "display": c.get("display") or key,
                        "trusted": bool(c.get("auto_send"))}
    if want in ("me", "myself", "note to self", "aman"):
        return {"kind": "number", "id": acct, "display": "Note to Self", "trusted": True}
    groups = rpc("listGroups") if daemon_up() else json.loads(_cli("-a", acct, "-o", "json", "listGroups") or "[]")
    for g in groups:
        if (g.get("name") or "").lower() == want or want in (g.get("name") or "").lower():
            return {"kind": "group", "id": g["id"], "display": g["name"], "trusted": False}
    people = rpc("listContacts") if daemon_up() else json.loads(_cli("-a", acct, "-o", "json", "listContacts") or "[]")
    for p in people:
        names = [p.get("name"), p.get("profile", {}).get("givenName") if isinstance(p.get("profile"), dict) else None,
                 p.get("profileName")]
        if any(n and want in n.lower() for n in names) and p.get("number"):
            return {"kind": "number", "id": p["number"], "display": next(n for n in names if n), "trusted": False}
    raise SignalError(f"no Signal contact or group matches {name!r}")


def send(name: str, text: str, confirmed: bool = False) -> dict:
    acct = account()
    to = resolve(name, acct)
    if not (to["trusted"] or confirmed):
        return {"sent": False, "needs_confirmation": True, "to": to["display"], "text": text}
    if daemon_up():   # the daemon holds the account; the CLI can't run alongside it
        params = {"groupId": to["id"]} if to["kind"] == "group" else {"recipient": [to["id"]]}
        ts = (rpc("send", message=text, **params) or {}).get("timestamp")
    else:
        target = ["-g", to["id"]] if to["kind"] == "group" else [to["id"]]
        ts = _cli("-a", acct, "send", "-m", text, *target).strip().splitlines()[-1]
    return {"sent": True, "to": to["display"], "text": text, "timestamp": ts}
