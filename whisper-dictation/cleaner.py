"""Text cleanup — GPT-4o-mini with per-app tone, or raw-pass in local mode."""

import os
import re
import logging
from typing import Optional

from openai import OpenAI

import settings as S
import stats as _stats

log = logging.getLogger(__name__)


# Fast local check: does the text contain common filler/disfluency words?
# If not, we can skip the expensive GPT call entirely.
_FILLER_RE = re.compile(
    r"\b("
    r"um|uh|uhm|erm|like|you know|i mean|sort of|kind of|"
    r"ну|эм|э+|мм+|типа|короче|это самое|в общем|как бы|вот|значит"
    r")\b",
    re.IGNORECASE,
)


_TONE_INSTRUCTIONS = {
    S.TONE_NEUTRAL:      "",
    S.TONE_PROFESSIONAL: "Use a professional, polite tone suitable for business communication.",
    S.TONE_CASUAL:       "Use a casual, friendly tone. Contractions are fine.",
    S.TONE_RAW:          None,  # skip cleanup entirely
}


def _build_system_prompt(tone: str, always_english: bool, user_style: str) -> str:
    base = (
        "You are a text cleanup assistant. The user will send you a raw voice "
        "transcription. Your job is to:\n"
        "- Remove filler words (um, uh, like, you know, эм, ну, короче, типа, etc.)\n"
        "- Fix obvious grammar mistakes caused by speech-to-text errors\n"
        "- Keep the original meaning and language exactly as spoken\n"
        "- Do NOT rephrase, summarize, or change the style\n"
        "- Do NOT add punctuation that wasn't implied\n"
        "- Return ONLY the cleaned text, nothing else"
    )

    tone_instruction = _TONE_INSTRUCTIONS.get(tone, "")
    extras = []
    if tone_instruction:
        extras.append(tone_instruction)
    if always_english:
        extras.append("Translate the text to English if it is in another language.")
    if user_style:
        extras.append(f"User style note: {user_style}")

    if extras:
        return base + "\n\nAdditional instructions:\n" + "\n".join(f"- {e}" for e in extras)
    return base


def _resolve_tone(bundle_id: Optional[str] = None) -> str:
    """Resolve effective tone: per-app override or base_tone from settings."""
    if bundle_id:
        app_tones = S.get("app_tones", {})
        if bundle_id in app_tones:
            return app_tones[bundle_id]
    return S.get("base_tone", S.TONE_NEUTRAL)


# Skip GPT cleanup for short phrases — filler words rarely appear
# in short dictations and the network round-trip costs ~2s.
_SHORT_PHRASE_MAX_WORDS = 4


def clean_text(raw_text: str, bundle_id: Optional[str] = None) -> str:
    """Clean up raw transcription using GPT-4o-mini.

    Respects settings:
    - mode=local: skip cleanup, return raw text
    - cleanup_enabled=False: skip cleanup
    - tone=raw: skip cleanup
    - otherwise: call OpenAI API with appropriate tone prompt

    Falls back to raw_text if the API call fails.
    """
    if not raw_text.strip():
        return raw_text

    mode = S.get("mode", S.MODE_AUTO)
    cleanup_enabled = S.get("cleanup_enabled", True)
    tone = _resolve_tone(bundle_id)

    # Skip cleanup if:
    # - user disabled it globally
    # - tone is raw (code editors, etc.)
    # - mode is local (no cloud LLM locally available here)
    if not cleanup_enabled:
        log.info("Cleanup disabled, returning raw text")
        return raw_text
    if tone == S.TONE_RAW:
        log.info("Raw tone (app: %s), skipping cleanup", bundle_id)
        return raw_text
    if mode == S.MODE_LOCAL:
        log.info("Local mode — skipping GPT cleanup, returning raw text")
        return raw_text

    # Skip cleanup for short phrases: saves ~2s of API round-trip,
    # and short phrases rarely contain filler words worth removing
    word_count = len(raw_text.split())
    if word_count <= _SHORT_PHRASE_MAX_WORDS:
        log.info("Short phrase (%d words) — skipping cleanup", word_count)
        return raw_text

    # Fast local check: if no filler words, skip cleanup entirely.
    # This saves ~2s on clean dictations (most of them).
    if not _FILLER_RE.search(raw_text):
        log.info("No filler words detected — skipping cleanup")
        return raw_text

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("No OPENAI_API_KEY, skipping cleanup")
        return raw_text

    try:
        client = OpenAI(api_key=api_key)
        system_prompt = _build_system_prompt(
            tone=tone,
            always_english=S.get("always_english", False),
            user_style=S.get("user_style", ""),
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.2,
            max_tokens=2048,
        )
        cleaned = response.choices[0].message.content.strip()

        # Record token usage for cost tracking
        try:
            usage = response.usage
            if usage is not None:
                _stats.record_gpt_tokens(
                    input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                )
        except Exception:
            pass

        if cleaned:
            log.info("Cleaned (tone=%s): %d -> %d chars", tone, len(raw_text), len(cleaned))
            return cleaned
    except Exception as e:
        log.warning("GPT cleanup failed, using raw text: %s", e)

    return raw_text
