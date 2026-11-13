#!/usr/bin/env bash
# Set up ~/.friday from this repo. Safe to re-run. Does NOT touch ~/.hermes or ~/.claude.
#   - links the Friday plugin into ~/.friday/plugins
#   - writes ~/.friday/config.yaml and SOUL.md (only if absent; pass --force to overwrite)
#   - builds the mic helper, installs the `friday` command into ~/.local/bin
#   - builds FridayEars.app into /Applications (it starts and stops fridayd + the Hermes gateway)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${FRIDAY_HOME:-$HOME/.friday}"
FORCE="${1:-}"
mkdir -p "$HOME_DIR"/{plugins,state,logs,bin,voices}

[ -f "$HOME_DIR/.env" ] || { echo "Missing $HOME_DIR/.env (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN)"; exit 1; }
BASE_URL="$(grep -E '^ANTHROPIC_BASE_URL=' "$HOME_DIR/.env" | cut -d= -f2-)"

ln -sfn "$REPO/plugins/friday" "$HOME_DIR/plugins/friday"

if [ ! -f "$HOME_DIR/config.yaml" ] || [ "$FORCE" = "--force" ]; then
  sed -e "s#\${ANTHROPIC_BASE_URL}#${BASE_URL}#" -e "s#@REPO@#${REPO}#g" "$REPO/config/hermes-config.yaml" > "$HOME_DIR/config.yaml"
  echo "wrote $HOME_DIR/config.yaml"
fi
if [ ! -s "$HOME_DIR/SOUL.md" ] || ! grep -q '^# Friday' "$HOME_DIR/SOUL.md" || [ "$FORCE" = "--force" ]; then
  cp "$REPO/config/SOUL.md" "$HOME_DIR/SOUL.md"
  echo "wrote $HOME_DIR/SOUL.md"
fi
[ -f "$HOME_DIR/friday.toml" ] || cp "$REPO/config/friday.toml" "$HOME_DIR/friday.toml"

swiftc -O -target arm64-apple-macos14.0 "$REPO/tools/micbusy.swift" -o "$HOME_DIR/bin/micbusy"
mkdir -p "$HOME/.local/bin" && ln -sfn "$REPO/bin/friday" "$HOME/.local/bin/friday"

[ -f "$HOME_DIR/voices/friday-ref.wav" ] || echo "Note: $HOME_DIR/voices/friday-ref.wav (Friday's voice reference) is missing."

"$REPO/scripts/brave-extension.sh"   # the Friday extension for Brave (load it once: brave://extensions)
"$REPO/scripts/build-ears.sh"   # FridayEars.app in /Applications: opening it starts Friday, quitting stops it
echo "Open /Applications/FridayEars.app to start Friday (add it to Login Items to start at login)."
echo
echo "Claude Code hooks are separate (they edit ~/.claude/settings.json):"
echo "  python3 $REPO/scripts/install-claude-hooks.py"
