"""Settings store — JSON-backed user preferences in ~/.whisper-dictation/settings.json."""

import json
import os
import logging
from typing import Dict, Any

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.whisper-dictation")
_SETTINGS_FILE = os.path.join(_CONFIG_DIR, "settings.json")

# Supported values
MODE_CLOUD = "cloud"
MODE_LOCAL = "local"
MODE_AUTO = "auto"  # cloud with local fallback (current behavior)

TONE_NEUTRAL = "neutral"
TONE_PROFESSIONAL = "professional"
TONE_CASUAL = "casual"
TONE_RAW = "raw"  # skip cleanup entirely

DEFAULTS: Dict[str, Any] = {
    "mode": MODE_AUTO,                 # cloud | local | auto
    "cleanup_enabled": True,           # run GPT cleanup pass
    "base_tone": TONE_NEUTRAL,         # default tone for cleanup
    "app_tones": {},                   # bundle_id -> tone override
    "force_builtin_mic": True,         # prefer laptop mic over Bluetooth
    "vad_enabled": True,               # strip silence before transcription
    "always_english": False,           # translate to English
    "user_style": "",                  # free-form user style hint
    "restore_clipboard": False,        # restore previous clipboard after paste
    "check_focus": True,               # check AX focus before paste
    "hotkey": "right_option",          # right_option | left_option | right_cmd |
                                       # caps_lock | right_shift | f13..f19
}


_cache = None


def _ensure_dir() -> None:
    os.makedirs(_CONFIG_DIR, exist_ok=True)


def load() -> Dict[str, Any]:
    """Load settings from disk (cached)."""
    global _cache
    if _cache is not None:
        return _cache

    if not os.path.exists(_SETTINGS_FILE):
        _cache = dict(DEFAULTS)
        return _cache

    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults so new keys work on upgrade
        merged = dict(DEFAULTS)
        merged.update(data)
        _cache = merged
        return _cache
    except Exception as e:
        log.error("Failed to load settings: %s — using defaults", e)
        _cache = dict(DEFAULTS)
        return _cache


def save(settings: Dict[str, Any]) -> None:
    """Save settings and invalidate cache."""
    global _cache
    _ensure_dir()
    try:
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        _cache = dict(settings)
        log.info("Settings saved")
    except Exception as e:
        log.error("Failed to save settings: %s", e)


def get(key: str, default: Any = None) -> Any:
    """Get a single setting value."""
    return load().get(key, DEFAULTS.get(key, default))


def set(key: str, value: Any) -> None:
    """Set and persist a single value."""
    s = load()
    s[key] = value
    save(s)


def reload() -> None:
    """Force reload from disk."""
    global _cache
    _cache = None
    load()
