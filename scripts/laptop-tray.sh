#!/usr/bin/env bash
# Copy Friday Voice (the Windows tray indicator, tools/FridayVoiceWindows) to the laptop.
#   laptop-tray.sh            copy to C:\Users\asus\FridayVoice
#   laptop-tray.sh snapshot   also render the card into /tmp/friday-voice-card.png (6 s of samples)
# Windows PowerShell 5 reads files without a BOM as ANSI ("·" becomes "Â·"), so files go over as UTF-8 with
# a BOM and CRLF line endings. Starting it at login is a separate, explicit step (see the README note in
# the script header).
set -euo pipefail
HOST="${FRIDAY_WIN_HOST:-winhome}"
SRC="$(cd "$(dirname "$0")/../tools/FridayVoiceWindows" && pwd)"
TMP="$(mktemp -d)"
python3 - "$SRC" "$TMP" <<'PY'
import sys
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[2])
for f in src.iterdir():
    if f.suffix in (".ps1", ".vbs"):
        text = f.read_text().replace("\r\n", "\n").replace("\n", "\r\n")
        (out / f.name).write_bytes((b"\xef\xbb\xbf" if f.suffix == ".ps1" else b"") + text.encode("utf-8" if f.suffix == ".ps1" else "ascii", "replace"))
PY
ssh -o BatchMode=yes "$HOST" "if not exist FridayVoice mkdir FridayVoice" 2>/dev/null
scp -q "$TMP"/* "$HOST":FridayVoice/ 2>/dev/null
echo "copied to $HOST:FridayVoice"
if [ "${1:-}" = "snapshot" ]; then
  ssh -o BatchMode=yes "$HOST" 'powershell -NoProfile -ExecutionPolicy Bypass -File FridayVoice\FridayVoice.ps1 -Snapshot %USERPROFILE%\FridayVoice\card.png' 2>/dev/null
  scp -q "$HOST":FridayVoice/card.png /tmp/friday-voice-card.png 2>/dev/null && echo "card: /tmp/friday-voice-card.png"
fi
rm -rf "$TMP"
