"""Text cleanup module — uses GPT-4o-mini to clean raw transcription."""

import os
import logging

from openai import OpenAI

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a text cleanup assistant. The user will send you a raw voice "
    "transcription. Your job is to:\n"
    "- Remove filler words (um, uh, like, you know, эм, ну, короче, типа, etc.)\n"
    "- Fix obvious grammar mistakes caused by speech-to-text errors\n"
    "- Keep the original meaning and language exactly as spoken\n"
    "- Do NOT rephrase, summarize, or change the style\n"
    "- Do NOT add punctuation that wasn't implied\n"
    "- Return ONLY the cleaned text, nothing else"
)


def clean_text(raw_text: str) -> str:
    """Clean up raw transcription text using GPT-4o-mini.

    Falls back to raw_text if the API call fails or returns empty.
    """
    if not raw_text.strip():
        return raw_text

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("No OPENAI_API_KEY, skipping cleanup")
        return raw_text

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.3,
            max_tokens=2048,
        )
        cleaned = response.choices[0].message.content.strip()
        if cleaned:
            log.info("Text cleaned: %d -> %d chars", len(raw_text), len(cleaned))
            return cleaned
    except Exception as e:
        log.warning("GPT cleanup failed, using raw text: %s", e)

    return raw_text
