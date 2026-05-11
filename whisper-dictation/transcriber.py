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
import api_status
from anti_hallucination import filter_transcription


# When the OpenAI client encounters 429/quota errors, it transparently
# retries with backoff (~3.5s per failed model). We disable that here:
# we have our own model-fallback loop, so a single failure should bubble
# up immediately. The circuit breaker handles the "API is down" case
# by skipping the API entirely on subsequent calls.
_API_MAX_RETRIES = 0

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
    # max_retries=0 — we own the fallback strategy and want failures to
    # surface in <1s instead of waiting through OpenAI's default 3 retries.
    return OpenAI(api_key=api_key, max_retries=_API_MAX_RETRIES)


def _get_local_model():
    """Lazy-load the local faster-whisper model.

    Sizing tradeoff for CPU inference (M-series Air, no GPU):
        tiny    ~ 1.5x realtime,  worst quality (drops words)
        base    ~ 1.0x realtime,  weak Russian
        small   ~ 0.4x realtime,  good Russian, ~3-4s for 10s clip ✓
        medium  ~ 0.1x realtime,  best quality but 12s+ for 13s clip — UNUSABLE
    We pick `small` as the sweet spot. The previous `medium` config was
    set when we expected the API to handle most calls — now that the
    breaker can keep us on local for 5 min stretches, latency dominates.
    """
    global _local_model
    if _local_model is None:
        try:
            from faster_whisper import WhisperModel
            model_size = S.get("local_model_size", "small")
            log.info("Loading local faster-whisper model (%s, int8)...", model_size)
            _local_model = WhisperModel(model_size, device="cpu", compute_type="int8")
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


# Model strategy:
#   - gpt-4o-transcribe: best accuracy on Russian/mixed speech ($0.006/min)
#     BUT has a known bug — on long audio (>10s) it often returns only the
#     first detected utterance and silently drops the rest. Useless for
#     real dictation longer than a sentence or two.
#   - gpt-4o-mini-transcribe: same truncation issue, slightly worse quality.
#   - whisper-1: older, slightly lower quality on Russian, but RELIABLY
#     transcribes long audio end-to-end without dropping content.
#
# Therefore we route by audio duration:
#   ≤ _LONG_AUDIO_SECONDS: gpt-4o-transcribe (high quality, no truncation risk)
#   > _LONG_AUDIO_SECONDS: whisper-1 (no truncation, accept slight quality hit)
#
# Sanity-check: if the chosen model returns suspiciously few chars/sec for
# the audio length (< _MIN_CHARS_PER_SEC), we treat that as a truncation
# event and re-try with whisper-1.
_PRIMARY_MODEL = "gpt-4o-transcribe"
_FALLBACK_MODEL = "gpt-4o-mini-transcribe"
_LONG_AUDIO_MODEL = "whisper-1"
_LONG_AUDIO_SECONDS = 8.0
# Russian speech is typically 12-20 chars/sec at conversational pace.
# Below ~6 chars/sec for a >8s clip almost always means the model returned
# only its first segment — that's our truncation signal.
_MIN_CHARS_PER_SEC = 6.0


def _call_openai_transcribe(client, audio_path: str, model_name: str) -> Optional[str]:
    """Single API call. Returns transcript text or None on failure.

    Trips the global circuit breaker on quota/billing errors — the next
    transcription will skip the API entirely and go straight to local.
    """
    try:
        with open(audio_path, "rb") as f:
            response = client.audio.transcriptions.create(
                model=model_name,
                file=f,
            )
        text = (response.text or "").strip()
        duration = _audio_duration_seconds(audio_path)
        if duration > 0:
            _stats.record_transcribe(model_name, duration)
        log.info("Transcribed via %s (%d chars)", model_name, len(text))
        return text
    except Exception as e:
        log.warning("Transcription via %s failed: %s", model_name, e)
        # Quota errors trip the breaker; after this no more API attempts
        # in the current dictation, and subsequent dictations skip API.
        api_status.trip(e)
        return None


def _transcribe_api(audio_path: str) -> Optional[str]:
    # Global breaker — if the API is known-down (quota exhausted), don't
    # waste 10s walking the model fallback chain. Caller will use local.
    if api_status.is_tripped():
        log.info(
            "API circuit breaker open (%.0fs remaining) — skipping API",
            api_status.time_remaining(),
        )
        return None

    client = _get_openai_client()
    if not client:
        return None

    duration = _audio_duration_seconds(audio_path)
    is_long = duration > _LONG_AUDIO_SECONDS

    # Long audio: skip gpt-4o-* entirely, go straight to whisper-1.
    # gpt-4o-transcribe will reliably return a partial result on long
    # clips, which would defeat the chars/sec sanity check below.
    if is_long:
        log.info("Audio %.1fs > %.1fs — using %s for full-length transcription",
                 duration, _LONG_AUDIO_SECONDS, _LONG_AUDIO_MODEL)
        text = _call_openai_transcribe(client, audio_path, _LONG_AUDIO_MODEL)
        if text or api_status.is_tripped():
            # If breaker tripped during the call, don't try more models.
            return text
        # whisper-1 failed for non-quota reason — try mini as last resort
        return _call_openai_transcribe(client, audio_path, _FALLBACK_MODEL)

    # Short audio: try gpt-4o-transcribe → mini → whisper-1
    last_text: Optional[str] = None
    for model_name in (_PRIMARY_MODEL, _FALLBACK_MODEL, _LONG_AUDIO_MODEL):
        # Bail out of the fallback chain the moment we know the API is
        # down — saves ~7s of guaranteed-to-fail attempts.
        if api_status.is_tripped():
            log.info("Breaker tripped mid-chain — skipping remaining models")
            break
        text = _call_openai_transcribe(client, audio_path, model_name)
        if text is None:
            continue  # API error — try next
        if not text:
            log.info("%s returned empty, trying next model", model_name)
            last_text = text
            continue
        # Sanity-check: detect silent truncation. If we get < _MIN_CHARS_PER_SEC
        # for non-trivial audio, the model probably returned only its first
        # phrase. Fall back to whisper-1 which doesn't have this bug.
        if duration > 3.0 and model_name != _LONG_AUDIO_MODEL:
            cps = len(text) / duration
            if cps < _MIN_CHARS_PER_SEC:
                log.warning(
                    "%s returned %d chars for %.1fs (%.1f c/s) — looks truncated, "
                    "retrying with %s", model_name, len(text), duration, cps,
                    _LONG_AUDIO_MODEL,
                )
                last_text = text
                continue
        return text
    return last_text


def _transcribe_local(audio_path: str) -> Optional[str]:
    model = _get_local_model()
    if model is None:
        return None
    try:
        # Tuning for CPU latency (was beam_size=5):
        #   beam_size=1  → greedy decoding, ~2× faster, quality loss ≈ noise
        #   vad_filter=True → faster-whisper's built-in Silero VAD strips
        #     non-speech BEFORE decoding. We already do an external VAD pass,
        #     but enabling it here too prevents the model spending 5s
        #     hallucinating into trailing silence.
        #   condition_on_previous_text=False → stops the model from drifting
        #     when prior segment was misrecognized; also slightly faster.
        #   no_speech_threshold=0.6 → drop segments the model itself flags
        #     as probably-not-speech (cuts hallucinated tails on long clips).
        segments, _info = model.transcribe(
            audio_path,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        # Track local (free) usage so stats show how much ran offline
        duration = _audio_duration_seconds(audio_path)
        if duration > 0:
            _stats.record_transcribe(_stats.MODEL_LOCAL, duration)
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
