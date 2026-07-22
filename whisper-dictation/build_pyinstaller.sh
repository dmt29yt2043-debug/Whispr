#!/bin/bash
# Build the standalone Whisper Dictation.app with PyInstaller and install it.
#
# Why PyInstaller (not py2app): py2app is broken on macOS Tahoe, and running
# the app via /usr/bin/python3 gave the process the shared "Python" identity —
# which the menu-bar status item and TCC (Accessibility/Microphone) grants
# could not attach to. PyInstaller produces a self-contained Mach-O binary
# whose identity IS the bundle, so the icon shows and permissions stick.
#
# After a rebuild the executable's cdhash changes, so macOS re-prompts for
# Accessibility + Microphone once. That's expected.

set -e
cd "$(dirname "$0")"

APP="/Applications/Whisper Dictation.app"
BUILT="dist/Whisper Dictation.app"

echo "== Kill running instances =="
pkill -9 -f "MacOS/Whisper Dictation" 2>/dev/null || true
pkill -9 -f "whisper-dictation/app.py" 2>/dev/null || true
rm -f "$HOME/.whisper-dictation/app.lock"
sleep 1

echo "== PyInstaller build =="
rm -rf build dist
python3 -m PyInstaller whisper_dictation.spec --noconfirm --distpath ./dist --workpath ./build

echo "== Install to /Applications =="
[ -d "$APP" ] && rm -rf "$APP"
cp -R "$BUILT" "$APP"

echo "== Ad-hoc deep sign =="
codesign --force --deep --sign - "$APP"

echo "== Register with LaunchServices =="
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

echo "== Install Login Item (auto-start) =="
PLIST="$HOME/Library/LaunchAgents/com.snigirev.whisper-dictation.plist"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.snigirev.whisper-dictation</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/Whisper Dictation.app/Contents/MacOS/Whisper Dictation</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>/tmp/whispr-agent.out</string>
  <key>StandardErrorPath</key><string>/tmp/whispr-agent.out</string>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/com.snigirev.whisper-dictation" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" || true

echo "== Launch =="
open -a "$APP"

cat <<'MSG'

Done. The mic icon should appear in the menu bar.

If macOS re-prompts (the cdhash changed on rebuild), grant:
  - System Settings > Privacy & Security > Accessibility  -> Whisper Dictation
  - Microphone prompt on first dictation                  -> Allow
MSG
