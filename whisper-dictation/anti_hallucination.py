"""Anti-hallucination filter for Whisper output.

Whisper often hallucinates on silent/noisy audio:
- Standalone noise tokens: "[BLANK_AUDIO]", "(music)", "[Music playing]"
- "Thanks for watching!", "Subscribe to my channel"
- Word/phrase repetitions ("you you you you...")

This module strips noise markers and rejects repetition hallucinations.
"""

import re
import logging
from typing import List, Tuple

log = logging.getLogger(__name__)

# Noise markers to strip (bracketed non-speech)
_BRACKET_NOISE = re.compile(r"\[[^\]]*\]|\([^\)]*\)")

# Characters from scripts we don't expect. Whisper sometimes hallucinates
# a single CJK / Korean / Arabic glyph when the audio is short or unclear
# ("готово" → "어" was observed). We keep Latin + Cyrillic + common symbols
# and strip everything else.
_UNSUPPORTED_SCRIPT = re.compile(
    "["
    "\u3040-\u309F"   # Hiragana
    "\u30A0-\u30FF"   # Katakana
    "\u3400-\u4DBF"   # CJK Extension A
    "\u4E00-\u9FFF"   # CJK Unified Ideographs
    "\uAC00-\uD7AF"   # Hangul Syllables
    "\u1100-\u11FF"   # Hangul Jamo
    "\u3130-\u318F"   # Hangul Compat Jamo
    "\u0600-\u06FF"   # Arabic
    "\u0700-\u074F"   # Syriac
    "\u0780-\u07BF"   # Thaana
    "\u0590-\u05FF"   # Hebrew
    "\u0E00-\u0E7F"   # Thai
    "\u0E80-\u0EFF"   # Lao
    "\u0F00-\u0FFF"   # Tibetan
    "\u0900-\u097F"   # Devanagari
    "\u0980-\u09FF"   # Bengali
    "\u0A00-\u0A7F"   # Gurmukhi
    "\u0C00-\u0C7F"   # Telugu
    "\u0D00-\u0D7F"   # Malayalam
    "\u0530-\u058F"   # Armenian
    "\u10A0-\u10FF"   # Georgian
    "]"
)

# Common Whisper hallucination phrases (case-insensitive substring match).
# These are things Whisper generates on silent / low-energy audio.
_HALLUCINATION_PHRASES = (
    "thanks for watching",
    "thank you for watching",
    "thank you very much",
    "thank you so much",
    "thank you for your attention",
    "thanks for listening",
    "thanks for tuning in",
    "subscribe to my channel",
    "please subscribe",
    "like and subscribe",
    "see you in the next video",
    "see you next time",
    "see you later",
    "don't forget to subscribe",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "ставьте лайк",
    "до скорых встреч",
    "всем пока",
    "до встречи",
)


def _strip_brackets(text: str) -> str:
    """Remove [..] and (..) noise markers."""
    return _BRACKET_NOISE.sub("", text).strip()


def _is_phrase_hallucination(text: str) -> bool:
    """Check if text is entirely a known hallucination phrase."""
    lowered = text.strip().lower().rstrip(".!?,")
    for phrase in _HALLUCINATION_PHRASES:
        # If text is exactly the phrase or very close to it
        if lowered == phrase:
            return True
        # If phrase dominates the text (>70%)
        if phrase in lowered and len(phrase) / max(len(lowered), 1) > 0.7:
            return True
    return False


def _is_repetition_hallucination(text: str) -> bool:
    """Detect 'you you you you...' style repetition hallucinations.

    Returns True if:
    - Single word is more than 60% of all words, OR
    - Any 2-3 word n-gram repeats more than 50% of the time
    """
    words = [w for w in re.split(r"\s+", text.strip().lower()) if w]
    if len(words) < 4:
        return False

    # Single word dominance
    from collections import Counter
    word_counts = Counter(words)
    top_word, top_count = word_counts.most_common(1)[0]
    if top_count / len(words) > 0.6:
        return True

    # 2-gram dominance
    if len(words) >= 4:
        bigrams = [(words[i], words[i + 1]) for i in range(len(words) - 1)]
        bg_counts = Counter(bigrams)
        top_bg, top_bg_count = bg_counts.most_common(1)[0]
        if top_bg_count / len(bigrams) > 0.5:
            return True

    # 3-gram dominance
    if len(words) >= 6:
        trigrams = [(words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2)]
        tg_counts = Counter(trigrams)
        top_tg, top_tg_count = tg_counts.most_common(1)[0]
        if top_tg_count / len(trigrams) > 0.5:
            return True

    return False


def _strip_unsupported_scripts(text: str) -> Tuple[str, int]:
    """Remove characters from scripts other than Latin/Cyrillic/digits/punct.

    Returns (cleaned_text, num_removed). We keep English + Russian and
    drop Korean, Japanese, Chinese, Arabic, etc. — Whisper sometimes
    hallucinates a single glyph in those scripts on unclear audio.
    """
    count = 0
    def _sub(m):
        nonlocal count
        count += len(m.group(0))
        return ""
    cleaned = _UNSUPPORTED_SCRIPT.sub(_sub, text)
    return cleaned, count


def filter_transcription(text: str) -> str:
    """Clean Whisper output. Returns cleaned text or empty string if hallucination."""
    if not text:
        return ""

    # 1. Strip noise markers
    cleaned = _strip_brackets(text)

    # 2. Collapse multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        log.info("Anti-hallucination: text was only noise markers, dropped")
        return ""

    # 3. Strip unsupported scripts (Korean/CJK/Arabic/etc. hallucinations).
    #    If the original was mostly unsupported, reject whole thing.
    orig_len = len(cleaned)
    cleaned, removed = _strip_unsupported_scripts(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if orig_len and removed / orig_len > 0.30:
        log.info(
            "Anti-hallucination: rejected — %d/%d chars were unsupported script",
            removed, orig_len,
        )
        return ""
    if not cleaned:
        log.info("Anti-hallucination: text was entirely unsupported script")
        return ""

    # 4. Check for known hallucination phrases
    if _is_phrase_hallucination(cleaned):
        log.info("Anti-hallucination: phrase hallucination detected: %r", cleaned[:60])
        return ""

    # 5. Check for repetition hallucinations
    if _is_repetition_hallucination(cleaned):
        log.info("Anti-hallucination: repetition detected: %r", cleaned[:80])
        return ""

    return cleaned
