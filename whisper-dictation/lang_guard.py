"""Detect transcription output that drifted into a wrong language.

The streaming model (gpt-realtime-whisper) occasionally mis-detects
Russian as a related Slavic language for part of an utterance and emits
its letters — Serbian (ј љ њ ћ ђ џ), Belarusian (ў і), Ukrainian (і ї є ґ),
or Latin-with-diacritics (š č ž ŭ …). Observed live: "Кажи моји задатак",
"Покажима и задаћан", "Tegni дашоў в этой задачы".

The user dictates ONLY Russian or English. So any alphabetic character
outside {basic Latin a-z/A-Z, Russian Cyrillic а-я/ё} is a reliable
signal that the model glitched. When that happens the caller re-runs the
SAME audio through the batch model (gpt-4o-transcribe), which is far more
robust and rarely drifts — fixing even the plain-ASCII garbage words
("Tegni") that a character filter alone can't repair.
"""

import unicodedata


def _is_allowed_letter(ch: str) -> bool:
    """True for basic-Latin (English) or Russian-Cyrillic letters only."""
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return True
    o = ord(ch)
    # Russian Cyrillic block А(0410)–я(044F) covers а-я/А-Я but NOT the
    # Serbian/Ukrainian/Belarusian extras (ј і ў ћ … live at 0450+ / 0400-040F).
    if 0x0410 <= o <= 0x044F:
        return True
    if ch in ("ё", "Ё"):
        return True
    return False


def foreign_letters(text: str) -> list:
    """Return the list of alphabetic chars that are neither English nor
    Russian. Empty list ⇒ text is pure RU/EN (plus digits/punct/emoji)."""
    out = []
    for ch in text or "":
        if ch.isalpha() and not _is_allowed_letter(ch):
            out.append(ch)
    return out


def is_suspicious(text: str, threshold: int = 1) -> bool:
    """True if the text contains >= threshold foreign-alphabet letters.

    threshold=1 by default: a single ј/ў/š never appears in correct
    Russian or English, so one is enough to trigger a batch re-transcribe.
    Digits, punctuation, whitespace and emoji are ignored (not alphabetic).
    """
    return len(foreign_letters(text)) >= threshold
