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
    assert path is None  # too short (<0.4s) OR silent
    # Second cycle works too
    r.start()
    time.sleep(0.05)
    path2 = r.stop()
    # both ok
    assert path2 is None or os.path.exists(path2)
    if path2 and os.path.exists(path2):
        os.unlink(path2)


@case("TC_011", "recorder", "recording <0.4s → returns None, no orphan WAV")
def test_too_short_returns_none():
    r = Recorder(force_builtin=False)
    r.start()
    time.sleep(0.02)
    path = r.stop()
    assert path is None


# ── Wake-from-sleep PortAudio reinit ─────────────────────────────────

@case("TC_REC_WAKE_FLAG", "recorder", "mark_subsystem_dirty sets the dirty flag")
def test_wake_flag_set():
    r = Recorder(force_builtin=False)
    assert r._subsystem_dirty is False
    r.mark_subsystem_dirty(reason="test")
    assert r._subsystem_dirty is True


@case("TC_REC_DIRTY_TRIGGERS_REINIT", "recorder",
      "wake → reinit happens (either in async warm-up OR at start()); dirty flag is cleared by end of start()")
def test_dirty_triggers_reinit():
    """Two paths can clear the dirty flag:
      (a) The background warm-up after mark_subsystem_dirty succeeds in
          opening a probe stream — clears the flag itself.
      (b) Warm-up fails (no real device in CI), flag stays set, then
          _start_locked reinits and clears the flag.
    Either way, PortAudio MUST get reinit'd before recording starts,
    and the flag MUST be False by the time start() returns.
    """
    r = Recorder(force_builtin=False)

    reinit_calls = []
    orig = Recorder._reinit_portaudio
    Recorder._reinit_portaudio = staticmethod(lambda: reinit_calls.append(1))
    try:
        r.mark_subsystem_dirty(reason="test")
        # Give the async warm-up a moment to run.
        time.sleep(0.2)
        r.start()
        time.sleep(0.02)
        r.stop()
    finally:
        Recorder._reinit_portaudio = orig

    assert len(reinit_calls) >= 1, (
        f"reinit must fire at least once after wake, got {len(reinit_calls)}"
    )
    assert r._subsystem_dirty is False, "dirty flag must be cleared after start"


@case("TC_REC_CLEAN_NO_REINIT", "recorder",
      "start() without dirty flag does NOT call PortAudio reinit (cheap path)")
def test_clean_skips_reinit():
    r = Recorder(force_builtin=False)
    assert r._subsystem_dirty is False

    reinit_calls = []
    orig = Recorder._reinit_portaudio
    Recorder._reinit_portaudio = staticmethod(lambda: reinit_calls.append(1))
    try:
        r.start()
        time.sleep(0.02)
        r.stop()
    finally:
        Recorder._reinit_portaudio = orig

    assert reinit_calls == [], "reinit should NOT fire on clean start"


@case("TC_REC_OPEN_RETRY_AFTER_FAILURE", "recorder",
      "InputStream open failure → reinit + retry; second attempt succeeds")
def test_open_retry_after_failure():
    """Simulates the classic post-wake -9986 error on the first open
    and a successful retry on the second. Without this, the user has
    to relaunch the app — see _open_input_with_retry in recorder.py."""
    import sounddevice as sd
    r = Recorder(force_builtin=False)

    attempts = {"n": 0}
    reinit_calls = []

    class _FakeStream:
        def start(self): pass
        def stop(self): pass
        def close(self): pass

    def fake_inputstream(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sd.PortAudioError("Internal PortAudio error [PaErrorCode -9986]")
        return _FakeStream()

    orig_input = sd.InputStream
    orig_reinit = Recorder._reinit_portaudio
    sd.InputStream = fake_inputstream
    Recorder._reinit_portaudio = staticmethod(lambda: reinit_calls.append(1))
    try:
        stream = r._open_input_with_retry(device=None, max_retries=2)
    finally:
        sd.InputStream = orig_input
        Recorder._reinit_portaudio = orig_reinit

    assert stream is not None, "second attempt should have succeeded"
    assert attempts["n"] == 2, f"expected 2 InputStream attempts, got {attempts['n']}"
    assert len(reinit_calls) == 1, (
        f"reinit should be called once between attempts, got {len(reinit_calls)}"
    )


@case("TC_REC_OPEN_GIVES_UP_AFTER_RETRIES", "recorder",
      "InputStream open fails on every attempt → returns None, no infinite loop")
def test_open_gives_up():
    import sounddevice as sd
    r = Recorder(force_builtin=False)

    attempts = {"n": 0}

    def always_fail(**kwargs):
        attempts["n"] += 1
        raise sd.PortAudioError("Internal PortAudio error [PaErrorCode -9986]")

    orig_input = sd.InputStream
    orig_reinit = Recorder._reinit_portaudio
    sd.InputStream = always_fail
    Recorder._reinit_portaudio = staticmethod(lambda: None)
    try:
        stream = r._open_input_with_retry(device=None, max_retries=2)
    finally:
        sd.InputStream = orig_input
        Recorder._reinit_portaudio = orig_reinit

    assert stream is None, "all-failures must return None, not raise"
    # max_retries=2 → 3 attempts total
    assert attempts["n"] == 3, f"expected 3 attempts total, got {attempts['n']}"


@case("TC_REC_SILENT_MARKS_DIRTY", "recorder",
      "silent recording (peak=rms=0) marks subsystem dirty for self-healing on next attempt")
def test_silent_marks_dirty():
    """Simulate the post-wake silent-capture scenario.

    We can't easily produce a real zero-only InputStream in a unit test,
    so we manipulate the recorder's frames directly to mimic a 1s
    all-zero recording, then call _stop_locked through the public stop().
    """
    r = Recorder(force_builtin=False)
    # Manually inject 1 second of silence as if the audio_callback
    # delivered zero buffers (the exact post-wake symptom).
    silent = np.zeros((recorder.SAMPLE_RATE, 1), dtype=np.float32)
    with r._lock:
        r._recording = True
        r._frames = [silent]
        r._stream = None  # no real stream
    path = r.stop()
    assert path is None, "silent recording must return None (not a WAV)"
    assert r._last_error == "mic_silent"
    assert r._subsystem_dirty is True, (
        "silent recording should mark subsystem dirty so next start() reinits PortAudio"
    )


if __name__ == "__main__":
    run_all("test_vad_and_recorder")
