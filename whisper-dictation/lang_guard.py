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

import re
import unicodedata


# ── Wrong Latin-script language detection ────────────────────────────
#
# The character filter below can't catch a drift into French/Spanish/etc.
# because those share the English a-z alphabet. Observed live: Russian
# speech transcribed as French "J'ai sur le file le jeu de Tiber Park"
# (all plain ASCII). We detect it by function-word markers + Romance
# elision patterns, applied ONLY to all-Latin text (text with Cyrillic
# is Russian and handled by the character filter).

_HAS_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# Romance elision: a lone letter (or "qu") + apostrophe + vowel — j'ai,
# c'est, l'eau, qu'il, d'un. English contractions never take this shape
# (their apostrophe follows a whole word: it's, don't, we'll), so this is
# a strong "not English" signal.
_ELISION = re.compile(r"\b(?:j|c|n|l|d|m|t|s|qu)['’][aeiouyàâäéèêëîïôöùûü]", re.I)

# High-frequency function words that are common in French/Spanish/Italian/
# German/Portuguese but essentially never appear in English or in a
# Russian+English dictation. Kept conservative to avoid flagging English.
_FOREIGN_MARKERS = frozenset({
    # French
    "je", "jeu", "jeux", "sur", "avec", "être", "très", "pour", "dans",
    "cette", "voilà", "bonjour", "oui", "aussi", "vous", "nous", "elle",
    "ils", "où", "déjà", "peut", "alors", "monsieur", "leur", "leurs",
    "mais", "faire", "chose", "beaucoup", "quoi", "quelque",
    # Spanish
    "hola", "gracias", "pero", "porque", "está", "esto", "esta", "para",
    "muy", "qué", "cómo", "señor", "año", "hacer", "tiene", "también",
    "entonces", "ahora", "aquí", "esa", "ese", "eso",
    # Italian
    "sono", "questo", "questa", "perché", "grazie", "ciao", "molto",
    "anche", "però", "essere", "tutto", "adesso", "allora", "sì",
    # German
    "ich", "und", "nicht", "das", "mit", "für", "aber", "sehr", "danke",
    "guten", "haben", "sein", "auch", "jetzt", "oder", "eine", "einen",
    # Portuguese
    "você", "obrigado", "não", "muito", "fazer", "agora", "então", "isso",
})

# A single one of these all but guarantees the wrong language.
_STRONG_MARKERS = frozenset({
    "jeu", "jeux", "avec", "être", "très", "voilà", "bonjour", "monsieur",
    "hola", "gracias", "señor", "porque", "grazie", "ciao", "perché",
    "danke", "guten", "obrigado", "você", "ich",
})


def looks_foreign_latin(text: str) -> bool:
    """True if all-Latin `text` looks like French/Spanish/German/etc.

    Only fires on text WITHOUT Cyrillic (Russian is handled elsewhere).
    English dictations — even ones with a stray foreign name — stay below
    the threshold; a Romance elision or a strong marker or >=2 markers is
    required.
    """
    if not text or _HAS_CYRILLIC.search(text):
        return False
    low = text.lower()
    if _ELISION.search(low):
        return True
    words = re.findall(r"[a-zà-ÿ']+", low)
    if any(w in _STRONG_MARKERS for w in words):
        return True
    hits = sum(1 for w in set(words) if w in _FOREIGN_MARKERS)
    return hits >= 2


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
    """True if `text` isn't plausibly Russian/English and should be
    re-transcribed via the robust batch model.

    Two independent signals:
      1. Foreign-alphabet letters (ј ў š …) — wrong Cyrillic or Latin
         with diacritics (Serbian/Belarusian/Czech drift).
      2. Foreign Latin-script LANGUAGE (French/Spanish/German/…) detected
         by function-word markers — catches all-ASCII drift the letter
         check can't see ("J'ai sur le jeu de …").
    """
    if len(foreign_letters(text)) >= threshold:
        return True
    return looks_foreign_latin(text)


def reason(text: str) -> str:
    """Short human-readable why-flagged string for logs ('' if clean)."""
    fl = foreign_letters(text)
    if fl:
        return "foreign letters: " + "".join(sorted(set(fl)))[:20]
    if looks_foreign_latin(text):
        return "foreign Latin-script language (French/Spanish/…)"
    return ""
