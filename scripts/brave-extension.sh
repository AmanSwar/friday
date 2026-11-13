#!/usr/bin/env bash
# Writes the Friday extension's config (bridge address + token) so it can reach fridayd.
# Then, once, in Brave: brave://extensions → Developer mode on → Load unpacked → extensions/brave
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$REPO/src" "$REPO/.venv/bin/python" - "$REPO/extensions/brave/config.js" <<'PY'
import json, os, sys
from friday_core.paths import BRIDGE_PORT, bridge_token
path = sys.argv[1]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("// Written by scripts/brave-extension.sh. Not committed: it holds the bridge token.\n"
             f"export const BRIDGE_URL = {json.dumps(f'ws://127.0.0.1:{BRIDGE_PORT}')};\n"
             f"export const TOKEN = {json.dumps(bridge_token())};\n")
print(f"wrote {path}")
PY
