#!/usr/bin/env python3
"""Connect Friday to Aman's iPhone and Apple Watch through the ntfy app.

    python3 scripts/phone-setup.py          # make the private topics (once), save them, send a test
    python3 scripts/phone-setup.py --test   # just send a test notification

Creates two unguessable topic names in ~/.friday/friday.toml under [phone] (notify: Friday -> phone;
reply: phone -> Friday) and prints what to do on the iPhone. Restart Friday afterwards (quit and reopen
FridayEars) so fridayd starts using them."""

import json
import re
import secrets
import sys
import urllib.request
from pathlib import Path

TOML = Path.home() / ".friday" / "friday.toml"
SERVER = "https://ntfy.sh"


def read_phone() -> dict:
    text = TOML.read_text() if TOML.exists() else ""
    m = re.search(r"^\[phone\]\s*$(.*?)(?=^\[|\Z)", text, re.S | re.M)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, _, v = line.partition("=")
        if _ and k.strip() and not k.strip().startswith("#"):
            out[k.strip()] = v.split("#")[0].strip().strip('"')
    return out


def write_phone(values: dict) -> None:
    text = TOML.read_text() if TOML.exists() else ""
    def fmt(v):   # booleans stay bare TOML booleans; "false" in quotes would read as on
        return str(v).lower() if isinstance(v, bool) or str(v).lower() in ("true", "false") else f'"{v}"'
    block = "[phone]\n" + "".join(f"{k} = {fmt(v)}\n" for k, v in values.items())
    if re.search(r"^\[phone\]\s*$", text, re.M):
        text = re.sub(r"^\[phone\]\s*$(.*?)(?=^\[|\Z)", block + "\n", text, flags=re.S | re.M)
    else:
        text = text.rstrip() + "\n\n" + block
    TOML.write_text(text)


def send(topic: str, title: str, message: str) -> None:
    body = json.dumps({"topic": topic, "title": title, "message": message, "tags": ["wave"]}).encode()
    urllib.request.urlopen(urllib.request.Request(SERVER, data=body, headers={"Content-Type": "application/json"}),
                           timeout=10).read()


def main() -> None:
    phone = read_phone()
    if "--test" not in sys.argv:
        phone.setdefault("ntfy_server", SERVER)
        if not phone.get("ntfy_topic"):
            phone["ntfy_topic"] = "friday-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
        if not phone.get("reply_topic"):
            phone["reply_topic"] = "friday-r-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
        phone.setdefault("command", "")
        phone["enabled"] = True   # running setup means the phone is ready: turn the link on
        write_phone(phone)
    send(phone["ntfy_topic"], "Friday", "If you can read this on your iPhone (and your watch), Friday can reach you.")
    reply = f"{SERVER}/{phone['reply_topic']}"
    print(f"""Saved in {TOML} ([phone]). Treat both topic names like passwords.

On the iPhone:
1. Install "ntfy" from the App Store; allow notifications. Apple Watch mirrors them automatically.
2. In ntfy: + (subscribe) -> topic {phone['ntfy_topic']}  (server ntfy.sh). The test message should appear.
3. To talk to Friday from the phone: subscribe to {phone['reply_topic']} too and post messages there.
   Friday answers as a notification. Permission prompts arrive with Allow / Deny buttons, which also
   work from the watch.

Health (Apple Watch, plus the Huawei band if Huawei Health syncs into Apple Health):
In Shortcuts -> Automation -> Time of Day (07:30, daily, Run Immediately), add these actions:
  - Find Health Samples: Sleep Analysis, start date in the last 1 day -> Calculate Statistics: Sum of Duration (hours)
  - Find Health Samples: Steps, start date yesterday -> Calculate Statistics: Sum
  - Find Health Samples: Resting Heart Rate, sorted latest first, limit 1
  - Find Health Samples: Heart Rate Variability, sorted latest first, limit 1
  - Text:  health {{"sleep_h": <sleep sum>, "steps_yesterday": <steps sum>, "resting_hr": <resting hr>, "hrv_ms": <hrv>}}
  - Get Contents of URL: {reply}   Method POST, Request Body: File -> the Text above
Friday stores it on the Mac (~/.friday/state/health.jsonl), mentions anything notable in your morning
briefing, and answers "how did I sleep".

Then quit and reopen FridayEars.""")


if __name__ == "__main__":
    main()
