#!/usr/bin/env bash
# Build FridayEars.app into /Applications (menu-bar helper: hold Right Option to talk to Friday).
# macOS ties Accessibility / Input Monitoring grants to the app's signature; an ad-hoc rebuild can
# invalidate them, so after rebuilding toggle FridayEars off and on in System Settings if it stops working.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${FRIDAY_EARS_APP:-/Applications/FridayEars.app}"
mkdir -p "$APP/Contents/MacOS" "${FRIDAY_HOME:-$HOME/.friday}/logs"
swiftc -O -target arm64-apple-macos14.0 -framework Cocoa "$REPO"/tools/FridayEars/*.swift -o "$APP/Contents/MacOS/FridayEars"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>dev.friday.ears</string>
  <key>CFBundleName</key><string>FridayEars</string>
  <key>CFBundleExecutable</key><string>FridayEars</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>FridayRepo</key><string>@REPO@</string>
  <key>NSAppleEventsUsageDescription</key><string>Friday controls apps when you ask: Spotify playback, writing in Apple Notes, quitting an app, and sending prompts into VS Code.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>After Friday answers, she listens for a few seconds so you can reply without holding the key. Recognition stays on this Mac when it can.</string>
  <key>NSMicrophoneUsageDescription</key><string>Friday listens for a few seconds after she answers, so you can reply without holding the key.</string>
  <key>NSCameraUsageDescription</key><string>While you're away from the keyboard, Friday checks (on-device, nothing is saved) whether you're back at your desk, so it can greet you.</string>
</dict>
</plist>
EOF
sed -i '' "s#@REPO@#$REPO#" "$APP/Contents/Info.plist"
# A stable identity keeps macOS's Accessibility / Input Monitoring grants across rebuilds; an ad-hoc
# signature changes every build and silently revokes them. Uses your Apple Development certificate if
# you have one; otherwise create one once in Keychain Access:
#   Keychain Access → Certificate Assistant → Create a Certificate… → Name "Friday Local Signing",
#   Identity Type "Self Signed Root", Certificate Type "Code Signing".
IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | grep -o '"\(Friday Local Signing\|Apple Development:[^"]*\)"' | head -1 | tr -d '"')"
if [ -n "$IDENTITY" ]; then
  codesign --force --sign "$IDENTITY" --identifier dev.friday.ears "$APP" >/dev/null
  echo "built $APP (signed: $IDENTITY; permissions survive rebuilds)"
else
  codesign --force --sign - --identifier dev.friday.ears "$APP" >/dev/null
  echo "built $APP (ad-hoc signed: re-grant Accessibility + Input Monitoring after each rebuild)"
fi
