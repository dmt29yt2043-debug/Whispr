#!/bin/bash
# Полная пересборка .app через py2app.
# ВНИМАНИЕ: это меняет codesign подпись → TCC теряет разрешения.
# После этого нужно заново дать:
#   - Accessibility
#   - Input Monitoring
#   - Microphone
#
# Использовать только когда меняются зависимости или Info.plist.
# Для изменений в .py коде используй ./update_app.sh

set -e
cd "$(dirname "$0")"

echo "== Kill running app =="
pkill -9 -f "Whisper Dictation" 2>/dev/null || true
sleep 1

echo "== py2app build =="
rm -rf build dist
python3 setup.py py2app 2>&1 | tail -3

echo "== Install to /Applications =="
rm -rf "/Applications/Whisper Dictation.app"
cp -r "dist/Whisper Dictation.app" /Applications/

echo "== Ad-hoc codesign =="
codesign --force --deep --sign - "/Applications/Whisper Dictation.app" 2>&1 | tail -1

echo "== Register with LaunchServices =="
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "/Applications/Whisper Dictation.app"

echo "== Launch =="
open -a "Whisper Dictation"

cat <<'MSG'

╔══════════════════════════════════════════════════════════════════╗
║ IMPORTANT: grant permissions again                               ║
║                                                                  ║
║ 1. System Settings → Privacy & Security:                         ║
║    - Accessibility    → remove old + add /Applications/...       ║
║    - Input Monitoring → remove old + add /Applications/...       ║
║    - Microphone       → will be prompted on first recording      ║
║                                                                  ║
║ 2. Restart the app from Launchpad after granting.                ║
╚══════════════════════════════════════════════════════════════════╝
MSG
