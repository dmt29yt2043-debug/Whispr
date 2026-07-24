"""Repair Russian words the model accidentally rendered in Latin script.

The transcription model occasionally mis-detects Russian as a related
Slavic language (Czech/Slovak) for a single word and emits it in Latin
transliteration WITH háček/caron diacritics — e.g. "Душа" → "Duša",
"Спасибо" → "Spasibo" is plain-ASCII (harder), but "душа/чаша/жизнь"
often come out as "duša/čaša/žiznь".

Key signal: the user dictates ONLY Russian or English. English never
uses háček characters (š č ž ě ř ň ď ť ľ …) and correctly-transcribed
Russian is Cyrillic. So any WORD containing one of these diacritics is,
with very high confidence, a mis-transliterated Russian word — and we
reverse-transliterate just that word back to Cyrillic. Plain-ASCII
English words are never touched (they contain no trigger diacritic).
"""

import re
import logging

log = logging.getLogger(__name__)

# Diacritics that mark a mis-transliterated Slavic (Russian) word.
# Deliberately excludes plain acutes (é á í ó ú) which show up in
# French/Spanish loanwords and are more ambiguous — the háček family and
# ë/è/ъ/ь-markers are the reliable "this was Cyrillic" tell.
_TRIGGER = "šČčžŽěĚřŘňŇďĎťŤľĽśŚźŹćĆńŃëËèÈ"
_TRIGGER_RE = re.compile("[" + re.escape(_TRIGGER) + "]")

# Reverse scientific-transliteration map (Latin → Cyrillic). Digraphs
# first, then single chars. Uppercase handled by capitalizing the result.
_DIGRAPHS = [
    ("šč", "щ"), ("Šč", "Щ"), ("ŠČ", "Щ"),
    ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"),
    ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("yo", "ё"),
]

_SINGLE = {
    "a": "а", "b": "б", "c": "ц", "č": "ч", "d": "д", "ď": "дь",
    "e": "е", "ë": "ё", "è": "э", "ě": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "ľ": "ль",
    "m": "м", "n": "н", "ň": "нь", "o": "о", "p": "п", "q": "к",
    "r": "р", "ř": "рь", "s": "с", "š": "ш", "ś": "с", "t": "т",
    "ť": "ть", "u": "у", "ů": "у", "v": "в", "w": "в", "x": "кс",
    "y": "ы", "z": "з", "ž": "ж", "ź": "з", "ć": "ч", "ń": "нь",
}


def _translit_word(word: str) -> str:
    """Reverse-transliterate a single Latin(+diacritics) word to Cyrillic."""
    was_title = word[:1].isupper()
    low = word.lower()

    # Apply digraphs first (longest-match), then single chars.
    i = 0
    out = []
    while i < len(low):
        matched = False
        for lat, cyr in _DIGRAPHS:
            lat_l = lat.lower()
            if low.startswith(lat_l, i):
                out.append(cyr)
                i += len(lat_l)
                matched = True
                break
        if matched:
            continue
        ch = low[i]
        out.append(_SINGLE.get(ch, ch))
        i += 1

    result = "".join(out)
    if was_title and result:
        result = result[:1].upper() + result[1:]
    return result


# A "word" for our purposes: a run of letters (Latin+diacritics), possibly
# with an internal apostrophe. Punctuation/spaces are preserved as-is.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def repair(text: str) -> str:
    """Convert any Latin-with-Slavic-diacritics words to Cyrillic.

    Words without a trigger diacritic are left untouched, so pure English
    and already-Cyrillic Russian pass through unchanged.
    """
    if not text or not _TRIGGER_RE.search(text):
        return text

    def _sub(m):
        w = m.group(0)
        if _TRIGGER_RE.search(w):
            fixed = _translit_word(w)
            if fixed != w:
                log.info("Translit repair: %r → %r", w, fixed)
            return fixed
        return w

    return _WORD_RE.sub(_sub, text)
