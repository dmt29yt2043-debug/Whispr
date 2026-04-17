"""Voice Activity Detection — strip silence from WAV files before transcription.

Uses webrtcvad for frame-level VAD, then merges voiced frames with padding.
Helps reduce Whisper hallucinations on silence and cuts API costs.
"""

import logging
import tempfile
import wave
import os
from typing import Optional, List, Tuple

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

try:
    import webrtcvad
    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False
    log.warning("webrtcvad not available, VAD disabled")

# webrtcvad requires 16kHz mono 16-bit PCM
# Supported frame sizes: 10ms, 20ms, 30ms
_FRAME_MS = 30
_SAMPLE_RATE = 16000
_FRAME_SAMPLES = int(_SAMPLE_RATE * _FRAME_MS / 1000)

# Aggressiveness: 0 (least) to 3 (most aggressive in filtering non-speech)
_AGGRESSIVENESS = 2

# Padding around voiced segments (ms)
_PAD_MS = 200
_PAD_FRAMES = _PAD_MS // _FRAME_MS

# Merge voiced segments separated by less than this gap (ms)
_MERGE_GAP_MS = 300
_MERGE_GAP_FRAMES = _MERGE_GAP_MS // _FRAME_MS


def _read_wav_as_int16(path: str) -> Tuple[np.ndarray, int]:
    """Read WAV file and return int16 mono samples at 16kHz."""
    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)  # mono
    if sr != _SAMPLE_RATE:
        # Simple resample via linear interpolation
        n_new = int(len(data) * _SAMPLE_RATE / sr)
        data = np.interp(
            np.linspace(0, len(data), n_new, endpoint=False),
            np.arange(len(data)),
            data,
        )
    # Convert to int16
    data = np.clip(data * 32767, -32768, 32767).astype(np.int16)
    return data, _SAMPLE_RATE


def _frame_generator(samples: np.ndarray):
    """Yield frames of FRAME_SAMPLES from samples."""
    for i in range(0, len(samples) - _FRAME_SAMPLES + 1, _FRAME_SAMPLES):
        yield samples[i:i + _FRAME_SAMPLES]


def strip_silence(audio_path: str) -> Optional[str]:
    """Process a WAV file, strip silence, save to temp file.

    Returns path to new WAV file (or original if VAD disabled / no speech change).
    Returns None if no speech at all detected.
    """
    if not _VAD_AVAILABLE:
        return audio_path

    try:
        samples, sr = _read_wav_as_int16(audio_path)
    except Exception as e:
        log.warning("VAD: failed to read audio: %s", e)
        return audio_path

    total_duration = len(samples) / sr
    if total_duration < 0.3:
        return audio_path  # too short, skip VAD

    vad = webrtcvad.Vad(_AGGRESSIVENESS)

    # Classify each frame
    voiced_flags: List[bool] = []
    frames: List[np.ndarray] = []
    for frame in _frame_generator(samples):
        frames.append(frame)
        try:
            is_speech = vad.is_speech(frame.tobytes(), sr)
        except Exception:
            is_speech = True  # fail open
        voiced_flags.append(is_speech)

    if not any(voiced_flags):
        log.info("VAD: no speech detected in %.2fs of audio", total_duration)
        return None

    # Merge gaps shorter than MERGE_GAP_FRAMES
    # Find runs of voiced frames with padding
    segments: List[Tuple[int, int]] = []  # (start_frame, end_frame)
    i = 0
    n = len(voiced_flags)
    while i < n:
        if not voiced_flags[i]:
            i += 1
            continue
        # Start of voiced segment
        start = i
        while i < n and voiced_flags[i]:
            i += 1
        end = i  # exclusive
        segments.append((start, end))

    # Merge adjacent segments if gap is small
    merged: List[Tuple[int, int]] = []
    for s, e in segments:
        if merged and s - merged[-1][1] <= _MERGE_GAP_FRAMES:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    # Add padding
    padded: List[Tuple[int, int]] = []
    for s, e in merged:
        s = max(0, s - _PAD_FRAMES)
        e = min(n, e + _PAD_FRAMES)
        padded.append((s, e))

    # Assemble output
    out_samples = []
    for s, e in padded:
        for i in range(s, e):
            out_samples.append(frames[i])
    if not out_samples:
        return audio_path
    out = np.concatenate(out_samples)

    out_duration = len(out) / sr
    removed = total_duration - out_duration
    log.info("VAD: %.2fs → %.2fs (removed %.2fs silence)", total_duration, out_duration, removed)

    if out_duration < 0.15:
        log.info("VAD: output too short after trim, returning None")
        return None

    # Write to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="vad_")
    sf.write(tmp.name, out, sr, subtype="PCM_16")
    return tmp.name
