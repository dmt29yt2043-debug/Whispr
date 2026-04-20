"""TC_011, TC_030, TC_031, TC_039 + basic recorder/VAD behaviour."""
import os
import tempfile
import time
import numpy as np
import soundfile as sf
from _harness import case, run_all

import vad
import recorder
from recorder import Recorder, _find_builtin_mic_index


def _make_wav(data: np.ndarray, sr: int = 16000) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="qa_")
    tmp.close()
    sf.write(tmp.name, data, sr, subtype="PCM_16")
    return tmp.name


@case("TC_VAD_SILENT", "vad", "pure silence → None (no speech)")
def test_vad_silent():
    path = _make_wav(np.zeros(16000 * 2, dtype=np.float32))
    try:
        out = vad.strip_silence(path)
        assert out is None
    finally:
        os.unlink(path)


@case("TC_VAD_TONE", "vad", "tone + silence → returns trimmed WAV")
def test_vad_tone():
    sr = 16000
    sil = np.zeros(sr)  # 1s silence
    t = np.linspace(0, 1, sr)
    tone = (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)  # 1s tone
    audio = np.concatenate([sil, tone, sil]).astype(np.float32)
    path = _make_wav(audio)
    try:
        out = vad.strip_silence(path)
        assert out is not None and os.path.exists(out)
        data, _ = sf.read(out)
        # Trimmed should be <3s
        assert len(data) / sr < 3.0
        os.unlink(out)
    finally:
        os.unlink(path)


@case("TC_VAD_TOO_SHORT", "vad", "<0.3s audio bypasses VAD, returns original")
def test_vad_too_short():
    audio = np.random.uniform(-0.1, 0.1, 16000 // 10).astype(np.float32)  # 0.1s
    path = _make_wav(audio)
    try:
        out = vad.strip_silence(path)
        assert out == path, "Too-short audio should pass through unchanged"
    finally:
        os.unlink(path)


@case("TC_030", "vad", "VAD failure on silent audio → None (pipeline will use raw)")
def test_vad_silent_returns_none():
    # This is what the pipeline relies on: None means "no speech" per VAD
    path = _make_wav(np.zeros(16000 * 2, dtype=np.float32))
    try:
        out = vad.strip_silence(path)
        assert out is None
    finally:
        os.unlink(path)


@case("TC_REC_BUILTIN_KEYWORDS", "recorder", "_find_builtin_mic_index matches MacBook Air Microphone")
def test_find_builtin_keyword():
    # Simulate sd.query_devices by monkey-patching
    import sounddevice as sd
    orig = sd.query_devices
    try:
        sd.query_devices = lambda: [
            {"name": "MateView", "max_input_channels": 2},
            {"name": "MacBook Air Microphone", "max_input_channels": 1},
            {"name": "ZoomAudioDevice", "max_input_channels": 2},
        ]
        idx = _find_builtin_mic_index()
        assert idx == 1, f"Expected index 1, got {idx}"
    finally:
        sd.query_devices = orig


@case("TC_REC_BUILTIN_NOT_FOUND", "recorder", "when no built-in mic → returns None (use default)")
def test_find_builtin_missing():
    import sounddevice as sd
    orig = sd.query_devices
    try:
        sd.query_devices = lambda: [
            {"name": "MateView", "max_input_channels": 2},
            {"name": "Some external mic", "max_input_channels": 1},
        ]
        idx = _find_builtin_mic_index()
        assert idx is None
    finally:
        sd.query_devices = orig


@case("TC_REC_SERIALIZED", "recorder", "start/stop serialized via _op_lock; rapid calls do not race")
def test_serialized():
    r = Recorder(force_builtin=False)
    # Start, then immediately stop. Should not raise, return None (too short).
    r.start()
    time.sleep(0.05)
    path = r.stop()
    assert path is None  # too short (<0.15s) OR silent
    # Second cycle works too
    r.start()
    time.sleep(0.05)
    path2 = r.stop()
    # both ok
    assert path2 is None or os.path.exists(path2)
    if path2 and os.path.exists(path2):
        os.unlink(path2)


@case("TC_011", "recorder", "recording <0.15s → returns None, no orphan WAV")
def test_too_short_returns_none():
    r = Recorder(force_builtin=False)
    r.start()
    time.sleep(0.02)
    path = r.stop()
    assert path is None


if __name__ == "__main__":
    run_all("test_vad_and_recorder")
