# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for Whisper Dictation.

Produces a fully self-contained .app whose executable is its OWN Mach-O
binary (not /usr/bin/python3). That single-identity bundle is what makes
the menu-bar status item and TCC (Accessibility / Microphone) grants
stick — the py2app path was broken on macOS Tahoe, and running via
python3 gave the process the shared "Python" identity.

Local faster-whisper offline fallback is intentionally NOT bundled here
(ctranslate2's native libs bloat the build and the code degrades
gracefully when the import fails). Streaming + batch cloud transcription
are the primary paths.

Build:  python3 -m PyInstaller whisper_dictation.spec --noconfirm
"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

_here = os.path.abspath(os.getcwd())

# Local application modules that are imported dynamically (some via
# `import x` inside functions) — list them so PyInstaller's static
# analysis doesn't miss them.
_local_modules = [
    "recorder", "transcriber", "cleaner", "injector", "replacements",
    "stats", "sounds", "hotkey", "overlay", "focus_check",
    "streaming_transcriber", "anti_hallucination", "vad", "settings",
    "api_status", "mic_icon", "history", "dictionary", "snippets",
    "repaste_hotkey",
]

# PyObjC submodules the app touches. collect_submodules keeps the
# framework bridges intact.
_hidden = list(_local_modules)
for pkg in ("objc", "Foundation", "AppKit", "Quartz", "AVFoundation"):
    _hidden += collect_submodules(pkg)
_hidden += collect_submodules("openai")
_hidden += ["rumps", "sounddevice", "soundfile", "webrtcvad", "_webrtcvad",
            "pyperclip", "dotenv", "websocket", "numpy"]

# webrtcvad ships as a top-level webrtcvad.py + a _webrtcvad*.so C
# extension. PyInstaller collected the .so but dropped the .py wrapper,
# so `import webrtcvad` failed at runtime (VAD silently disabled). Add
# the wrapper source explicitly as a data file into the app root.
try:
    import webrtcvad as _wv
    _datas_extra = [(_wv.__file__, ".")]
except Exception:
    _datas_extra = []

# Bundle the native audio dylibs' data packages so sounddevice/soundfile
# find libportaudio / libsndfile at runtime.
_datas = []
_datas += collect_data_files("_sounddevice_data")
_datas += collect_data_files("_soundfile_data")
_datas += _datas_extra
if os.path.exists(os.path.join(_here, "icon.icns")):
    _datas.append((os.path.join(_here, "icon.icns"), "."))

a = Analysis(
    ["app.py"],
    pathex=[_here],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
        "torch", "tensorflow", "pynput", "tkinter", "matplotlib",
        "PIL", "scipy", "pandas",
        # setuptools/pkg_resources are only build-time deps. PyInstaller's
        # pkg_resources runtime hook crashes when a sys.path entry contains
        # spaces (our project path does) — nothing in the app imports
        # pkg_resources at runtime, so drop it entirely and skip the hook.
        "setuptools", "pkg_resources", "pip",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Whisper Dictation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Whisper Dictation",
)

app = BUNDLE(
    coll,
    name="Whisper Dictation.app",
    icon="icon.icns",
    bundle_identifier="com.snigirev.whisper-dictation",
    version="1.0.0",
    info_plist={
        "CFBundleName": "Whisper Dictation",
        "CFBundleDisplayName": "Whisper Dictation",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSUIElement": True,                 # menu-bar only, no Dock icon
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "Whisper Dictation needs microphone access for voice recording.",
        "NSAppleEventsUsageDescription":
            "Whisper Dictation uses Accessibility for global hotkeys.",
        "NSRequiresAquaSystemAppearance": False,
    },
)
