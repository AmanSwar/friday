#!/usr/bin/env bash
# Link signal-cli to your Signal account as a device ("Friday Mac"), like Signal Desktop.
# Shows the link QR as an image (terminal QR art often scans badly) with no trailing newline in it
# (a newline inside the link makes Signal say "invalid QR code").
#   Phone: Signal → Settings → Linked devices → Link new device → scan the image that opens.
set -euo pipefail
LOG=/tmp/signal-link.log
PNG=/tmp/signal-link.png
rm -f "$LOG" "$PNG"
signal-cli link -n "Friday Mac" > "$LOG" 2>&1 &
pid=$!
for _ in $(seq 1 60); do grep -q 'sgnl://' "$LOG" && break; sleep 0.5; done
uri="$(grep -o 'sgnl://[^[:space:]]*' "$LOG" | head -1)"
if [ -z "$uri" ]; then echo "signal-cli didn't print a link:"; cat "$LOG"; kill "$pid" 2>/dev/null; exit 1; fi
echo "Link: ${uri:0:60}…"
qrencode -s 10 -m 4 -o "$PNG" "$uri"
open "$PNG"
echo "Scan the QR image now with Signal on your phone (it expires in a minute or two)…"
wait "$pid" || true
tail -3 "$LOG"
grep -q 'Associated with' "$LOG" && echo "Linked." || echo "Not linked: run this again and scan quickly."
