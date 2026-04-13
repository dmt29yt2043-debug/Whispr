"""Text replacement engine — maps trigger phrases to replacement text."""

import json
import os
import logging
from typing import Dict

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.whisper-dictation")
_REPLACEMENTS_FILE = os.path.join(_CONFIG_DIR, "replacements.json")


def _ensure_config_dir() -> None:
    os.makedirs(_CONFIG_DIR, exist_ok=True)


def load_replacements() -> Dict[str, str]:
    """Load replacements from JSON file. Returns {trigger_phrase: replacement_text}."""
    if not os.path.exists(_REPLACEMENTS_FILE):
        return {}
    try:
        with open(_REPLACEMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in data.items()}
    except Exception as e:
        log.error("Failed to load replacements: %s", e)
        return {}


def save_replacements(replacements: Dict[str, str]) -> None:
    """Save replacements to JSON file."""
    _ensure_config_dir()
    try:
        with open(_REPLACEMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(replacements, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save replacements: %s", e)


def apply_replacements(text: str) -> str:
    """Apply text replacements to the transcribed text.

    If the entire text (lowered, stripped) matches a trigger phrase,
    replace it entirely. Otherwise return text unchanged.
    """
    replacements = load_replacements()
    if not replacements:
        return text

    normalized = text.strip().lower()

    # Exact match — replace the whole text
    if normalized in replacements:
        replacement = replacements[normalized]
        log.info("Replacement triggered: '%s' -> '%s'", normalized, replacement)
        return replacement

    return text
