"""py2app build configuration for Whisper Dictation."""

import os
import site
from setuptools import setup

APP = ["app.py"]
APP_NAME = "Whisper Dictation"

# Find the portaudio and libsndfile dylibs that sounddevice/soundfile need
_site_packages = site.getusersitepackages()


def _find_dylib(pkg_name, lib_pattern):
    """Find a dylib inside a site-packages subdirectory."""
    pkg_dir = os.path.join(_site_packages, pkg_name)
    if os.path.isdir(pkg_dir):
        for root, dirs, files in os.walk(pkg_dir):
            for f in files:
                if f.endswith(".dylib") and lib_pattern in f:
                    return os.path.join(root, f)
    return None


# Collect dylibs to bundle as frameworks
_frameworks = []
_portaudio = _find_dylib("_sounddevice_data", "libportaudio")
if _portaudio:
    _frameworks.append(_portaudio)

_sndfile = _find_dylib("_soundfile_data", "libsndfile")
if _sndfile:
    _frameworks.append(_sndfile)

print(f"Bundling frameworks: {_frameworks}")

_here = os.path.dirname(os.path.abspath(__file__))
_icon = os.path.join(_here, "icon.icns")

OPTIONS = {
    "argv_emulation": False,
    "iconfile": _icon if os.path.exists(_icon) else None,
    "frameworks": _frameworks,
    "semi_standalone": False,  # full standalone — ship our own Python
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.whisper.dictation",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Whisper Dictation needs microphone access for voice recording.",
        "NSAppleEventsUsageDescription": "Whisper Dictation uses Accessibility for global hotkeys.",
        "NSRequiresAquaSystemAppearance": False,
        "LSMinimumSystemVersion": "12.0",
    },
    "includes": [
        "recorder", "transcriber", "cleaner", "injector", "replacements",
        "stats", "sounds", "hotkey", "overlay", "focus_check",
        "anti_hallucination", "vad", "settings",
        "rumps", "sounddevice", "_sounddevice_data", "soundfile",
        "numpy", "openai", "pyperclip", "dotenv", "webrtcvad",
        "Quartz", "AppKit", "Foundation", "objc", "AVFoundation",
    ],
    "packages": [
        "_sounddevice_data", "_soundfile_data",
        "faster_whisper", "ctranslate2", "huggingface_hub", "tokenizers",
    ],
    "excludes": ["pynput"],  # py2app boot always needs multiprocessing.spawn
}

setup(
    name=APP_NAME,
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
