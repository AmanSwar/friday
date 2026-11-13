#!/usr/bin/env bash
# signal-cli as a daemon for Friday: JSON-RPC + live events on 127.0.0.1:8080. Run by FridayEars.
# No read receipts are sent, so reading a chat never shows it as read.
set -euo pipefail
CLI="$(command -v signal-cli || echo /opt/homebrew/bin/signal-cli)"
ACCT="$("$CLI" listAccounts 2>/dev/null | grep -o '+[0-9]\{6,15\}' | head -1)"
[ -n "$ACCT" ] || { echo "no linked Signal account; run scripts/link-signal.sh"; exit 1; }
exec "$CLI" -a "$ACCT" daemon --http 127.0.0.1:8080 --receive-mode on-start \
  --ignore-attachments --ignore-stories --ignore-avatars --ignore-stickers
