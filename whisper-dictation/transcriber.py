"""Transcription module — OpenAI Whisper API with local faster-whisper fallback."""

import os
import logging

from typing import Optional

from openai import OpenAI

log = logging.getLogger(__name__)

_local_model = None


def _get_openai_client() -> Optional[OpenAI]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def _get_local_model():
    """Lazy-load the local faster-whisper model."""
    global _local_model
    if _local_model is None:
        try:
            from faster_whisper import WhisperModel
            log.info("Loading local faster-whisper model (medium, int8)...")
            _local_model = WhisperModel("medium", device="cpu", compute_type="int8")
            log.info("Local model loaded.")
        except Exception as e:
            log.error("Failed to load local whisper model: %s", e)
            return None
    return _local_model


def transcribe(audio_path: str) -> str:
    """Transcribe audio file. Tries OpenAI API first, falls back to local model."""
    # Try OpenAI API
    client = _get_openai_client()
    if client:
        try:
            with open(audio_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
            text = response.text.strip()
            if text:
                log.info("Transcribed via OpenAI API (%d chars)", len(text))
                return text
        except Exception as e:
            log.warning("OpenAI API failed, falling back to local model: %s", e)

    # Fallback to local model
    model = _get_local_model()
    if model is None:
        return ""

    try:
        segments, _info = model.transcribe(audio_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info("Transcribed via local model (%d chars)", len(text))
        return text
    except Exception as e:
        log.error("Local transcription failed: %s", e)
        return ""
