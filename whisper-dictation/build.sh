#!/bin/bash
set -e

echo "=== Whisper Dictation — Build macOS App ==="

# Install py2app if needed
pip3 install py2app 2>/dev/null || true

# Clean previous builds
rm -rf build dist

# Build the .app bundle
echo "Building .app..."
python3 setup.py py2app

echo ""
echo "=== Build complete! ==="
echo "App location: dist/Whisper Dictation.app"
echo ""

# Copy .env to config dir so the app can find it
mkdir -p ~/.whisper-dictation
if [ -f .env ] && [ ! -f ~/.whisper-dictation/.env ]; then
    cp .env ~/.whisper-dictation/.env
    echo "Copied .env to ~/.whisper-dictation/.env"
fi

# Ask to install
read -p "Copy to /Applications? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "/Applications/Whisper Dictation.app"
    cp -r "dist/Whisper Dictation.app" /Applications/
    echo "Installed to /Applications/Whisper Dictation.app"
    echo "You can now launch it from Applications or Spotlight!"
fi
