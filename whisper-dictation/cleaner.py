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
_FILLER_RE = re.compile(
    r"\b("
    r"um|uh|uhm|erm|like|you know|i mean|sort of|kind of|"
    r"ну|эм|э+|мм+|типа|короче|это самое|в общем|как бы|вот|значит"
    r")\b",
    re.IGNORECASE,
)

# Sentence-ending punctuation (used to detect unformatted dumps)
_SENTENCE_END_RE = re.compile(r"[.!?…]")


# Skip GPT cleanup for short phrases — filler words rarely appear there
# and GPT-call latency (~2s) isn't worth it.
_SHORT_PHRASE_MAX_WORDS = 4


def _needs_formatting(text: str) -> bool:
    """Long speech with no/few sentence breaks needs GPT to add punctuation
    and paragraph splits — otherwise the user gets a monolithic wall of text."""
    words = text.split()
    n_words = len(words)
    if n_words < 15:
        return False
    endings = len(_SENTENCE_END_RE.findall(text))
    if endings == 0:
        return True  # no punctuation at all — definitely needs formatting
    # Average sentence length above ~25 words = likely missing breaks
    return n_words / endings > 25


_TONE_INSTRUCTIONS = {
    S.TONE_NEUTRAL:      "",
    S.TONE_PROFESSIONAL: "Use a professional, polite tone suitable for business communication.",
    S.TONE_CASUAL:       "Use a casual, friendly tone. Contractions are fine.",
    S.TONE_RAW:          None,  # skip cleanup entirely
}


def _build_system_prompt(tone: str, always_english: bool, user_style: str) -> str:
    base = (
        "You are a text cleanup assistant. The user will send you a raw voice "
        "transcription (often one continuous chunk with no punctuation). "
        "Your job:\n"
        "- Add punctuation (periods, commas, question marks, colons) so the text reads naturally.\n"
        "- Split the text into sentences.\n"
        "- Insert paragraph breaks (blank lines) when the topic shifts.\n"
        "- Remove filler words (um, uh, like, you know, эм, ну, короче, типа, etc.).\n"
        "- Fix obvious grammar mistakes from speech-to-text errors.\n"
        "- Capitalize the first letter of each sentence.\n"
        "- Preserve the original meaning AND language (don't translate).\n"
        "- Do NOT rephrase, summarize, or change the style.\n"
        "- Do NOT add content the speaker didn't say.\n"
        "- Return ONLY the cleaned text, nothing else."
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

    # Skip cleanup for very short phrases ("done", "go back", etc.)
    word_count = len(raw_text.split())
    if word_count <= _SHORT_PHRASE_MAX_WORDS:
        log.info("Short phrase (%d words) — skipping cleanup", word_count)
        return raw_text

    # Trigger GPT if either:
    #   - text has filler words to remove, OR
    #   - text is long and poorly punctuated (needs sentence/paragraph breaks)
    has_fillers = bool(_FILLER_RE.search(raw_text))
    needs_format = _needs_formatting(raw_text)
    if not has_fillers and not needs_format:
        log.info("Clean + well-formatted text — skipping cleanup")
        return raw_text
    reasons = []
    if has_fillers: reasons.append("fillers")
    if needs_format: reasons.append("needs formatting")
    log.info("Cleanup triggered: %s (%d words)", ", ".join(reasons), word_count)

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
        # BUG FIX #14: content can be None when finish_reason='content_filter'
        content = response.choices[0].message.content
        if content is None:
            log.warning("GPT returned None content (likely content filter) — using raw text")
            return raw_text
        cleaned = content.strip()

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
