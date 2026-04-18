#!/bin/bash
# Обновление Python-кода внутри установленного .app БЕЗ пересборки py2app.
# Сохраняет codesign подпись → TCC не сбрасывает разрешения.
#
# Использовать ТОЛЬКО когда меняется .py код.
# Если менялись зависимости (requirements.txt, новые import-ы) — нужен
# полноценный rebuild через rebuild_app.sh.

set -e

cd "$(dirname "$0")"

APP="/Applications/Whisper Dictation.app"
BUNDLE_ZIP="$APP/Contents/Resources/lib/python39.zip"
STAGE=/tmp/whisper_stage_$$

if [ ! -f "$BUNDLE_ZIP" ]; then
    echo "ERROR: bundle not found at $BUNDLE_ZIP"
    echo "Run ./rebuild_app.sh first for initial setup."
    exit 1
fi

echo "== Kill running app =="
pkill -9 -f "Whisper Dictation" 2>/dev/null || true
sleep 1

echo "== Compile .py files =="
for f in *.py; do
    [[ "$f" == "setup.py" || "$f" == "make_icon.py" || "$f" == "install_icon.py" ]] && continue
    python3 -c "import py_compile; py_compile.compile('$f', cfile='/tmp/${f%.py}.pyc', doraise=True)"
done

echo "== Unzip current bundle =="
rm -rf "$STAGE"
mkdir -p "$STAGE"
cd "$STAGE"
unzip -q "$BUNDLE_ZIP"

echo "== Replace .pyc files =="
for f in "$OLDPWD"/*.py; do
    name=$(basename "$f")
    [[ "$name" == "setup.py" || "$name" == "make_icon.py" || "$name" == "install_icon.py" ]] && continue
    pyc_name="${name%.py}.pyc"
    if [ -f "/tmp/$pyc_name" ]; then
        cp "/tmp/$pyc_name" "$STAGE/$pyc_name"
    fi
done

echo "== Re-zip bundle =="
rm -f "$BUNDLE_ZIP"
zip -q -r "$BUNDLE_ZIP" .

cd "$OLDPWD"
rm -rf "$STAGE"

echo "== Copy app.py entry point =="
# py2app runs Contents/Resources/app.py as the entry module, NOT the .pyc
# in the zip. So we must also overwrite the source file.
cp app.py "$APP/Contents/Resources/app.py"

echo "== Relaunch =="
open -a "Whisper Dictation"
echo "Done. Icon should appear in menu bar within a few seconds."
