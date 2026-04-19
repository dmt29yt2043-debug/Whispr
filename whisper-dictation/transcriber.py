"""Transcription — OpenAI Whisper API, faster-whisper local, or auto-fallback.

Mode:
  - cloud: API only (fail if no internet/no key)
  - local: faster-whisper only (offline)
  - auto: try API first, fall back to local

Post-processing with anti-hallucination filter.
"""

import os
import logging
from typing import Optional

from openai import OpenAI

import settings as S
import stats as _stats
from anti_hallucination import filter_transcription

log = logging.getLogger(__name__)


def _audio_duration_seconds(path: str) -> float:
    """Return duration of a WAV file in seconds."""
    try:
        import soundfile as _sf
        info = _sf.info(path)
        if info.samplerate > 0:
            return info.frames / info.samplerate
    except Exception:
        pass
    return 0.0

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


def warmup_local_model() -> None:
    """Pre-load the local model (no-op if cloud mode)."""
    mode = S.get("mode", S.MODE_AUTO)
    if mode in (S.MODE_LOCAL, S.MODE_AUTO):
        _get_local_model()


# gpt-4o-mini-transcribe: newer, faster, cheaper than whisper-1 ($0.003/min vs $0.006/min).
# Falls back to whisper-1 if the newer model errors out.
_PRIMARY_MODEL = "gpt-4o-mini-transcribe"
_FALLBACK_MODEL = "whisper-1"


def _transcribe_api(audio_path: str) -> Optional[str]:
    client = _get_openai_client()
    if not client:
        return None

    last_text: Optional[str] = None
    for model_name in (_PRIMARY_MODEL, _FALLBACK_MODEL):
        try:
            with open(audio_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    model=model_name,
                    file=f,
                )
            text = (response.text or "").strip()
            duration = _audio_duration_seconds(audio_path)
            if duration > 0:
                _stats.record_whisper_seconds(duration)
            log.info("Transcribed via %s (%d chars)", model_name, len(text))

            # BUG FIX #15: empty string from the primary model also
            # counts as 'no result' — try the fallback before giving up.
            if text:
                return text
            if model_name != _FALLBACK_MODEL:
                log.info("Primary model returned empty, trying fallback %s", _FALLBACK_MODEL)
            last_text = text
        except Exception as e:
            log.warning("Transcription via %s failed: %s", model_name, e)

    return last_text  # may be "" if all models returned empty


def _transcribe_local(audio_path: str) -> Optional[str]:
    model = _get_local_model()
    if model is None:
        return None
    try:
        segments, _info = model.transcribe(audio_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
    except Exception as e:
        log.error("Local transcription failed: %s", e)
        return None


def transcribe(audio_path: str) -> str:
    """Transcribe audio file. Returns cleaned text (or empty if hallucination)."""
    mode = S.get("mode", S.MODE_AUTO)

    raw = None
    if mode == S.MODE_CLOUD:
        raw = _transcribe_api(audio_path)
    elif mode == S.MODE_LOCAL:
        raw = _transcribe_local(audio_path)
    else:  # auto
        raw = _transcribe_api(audio_path)
        if raw is None or not raw:
            log.info("Falling back to local transcription")
            raw = _transcribe_local(audio_path)

    if not raw:
        return ""

    log.info("Transcribed (mode=%s, %d chars): %r", mode, len(raw), raw[:80])

    # Anti-hallucination filter
    filtered = filter_transcription(raw)
    if filtered != raw:
        if not filtered:
            log.warning("Transcription rejected by anti-hallucination filter")
        else:
            log.info("Anti-hallucination cleaned: %r -> %r", raw[:60], filtered[:60])

    return filtered
