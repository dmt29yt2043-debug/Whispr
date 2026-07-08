"""Personal dictionary — user-specific terms that bias transcription.

Wispr Flow's "personal dictionary that learns your unique words" feature,
minus the auto-learning: the user (or the app menu) adds terms explicitly.

How it improves accuracy: Whisper-family models accept a text `prompt`
that conditions the decoder. Putting the user's names/brands/jargon in
the prompt makes the model prefer those exact spellings — e.g. "Whispr
Flow" instead of "Виспер флоу", "RIZY" instead of "ризи".

Storage: ~/.whisper-dictation/dictionary.txt, one term per line, UTF-8.
Lines starting with '#' are comments. The file can be edited by hand.

Used by:
  - transcriber._call_openai_transcribe → `prompt=` param (batch API;
    whisper-1 and gpt-4o-transcribe support it)
  - cleaner._build_system_prompt → "preserve these terms" instruction
  (- streaming GA gpt-realtime-whisper does NOT support prompts — the
    cleanup pass is the dictionary's safety net there)
"""

import logging
import os
import threading
from typing import List

log = logging.getLogger(__name__)

_DICT_PATH = os.path.expanduser("~/.whisper-dictation/dictionary.txt")

# Cap what we inject: Whisper prompts are limited (~224 tokens) and very
# long prompts dilute the biasing effect. 40 terms is plenty for names,
# brands and project jargon.
_MAX_TERMS = 40

_lock = threading.Lock()
_cache: List[str] = []
_cache_mtime: float = -1.0


def get_terms() -> List[str]:
    """Return dictionary terms (most recently added last). Cached by mtime."""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(_DICT_PATH)
    except OSError:
        return []
    with _lock:
        if mtime != _cache_mtime:
            terms: List[str] = []
            try:
                with open(_DICT_PATH, encoding="utf-8") as f:
                    for line in f:
                        t = line.strip()
                        if t and not t.startswith("#"):
                            terms.append(t)
            except OSError as e:
                log.warning("Dictionary read failed: %s", e)
                return list(_cache)
            _cache = terms
            _cache_mtime = mtime
        return list(_cache)


def add_term(term: str) -> bool:
    """Append a term (deduplicated, case-insensitive). Returns True if added."""
    term = (term or "").strip()
    if not term:
        return False
    existing = {t.lower() for t in get_terms()}
    if term.lower() in existing:
        log.info("Dictionary: %r already present", term)
        return False
    os.makedirs(os.path.dirname(_DICT_PATH), exist_ok=True)
    with _lock:
        with open(_DICT_PATH, "a", encoding="utf-8") as f:
            f.write(term + "\n")
    log.info("Dictionary: added %r", term)
    return True


def transcription_prompt() -> str:
    """Prompt string for the batch transcription API ('' when empty).

    Phrased as a vocabulary hint, not an instruction — Whisper prompts
    work by example, not by command. Keep it looking like natural text
    so the anti-hallucination prompt-echo filter can catch a verbatim
    echo (dominant-substring check) on silent audio.
    """
    terms = get_terms()[-_MAX_TERMS:]
    if not terms:
        return ""
    return "Glossary: " + ", ".join(terms) + "."


def cleanup_instruction() -> str:
    """Extra system-prompt line for the GPT cleanup pass ('' when empty)."""
    terms = get_terms()[-_MAX_TERMS:]
    if not terms:
        return ""
    return (
        "Preserve these user-specific terms spelled EXACTLY as written "
        "(correct near-miss transcriptions to them): " + ", ".join(terms)
    )
