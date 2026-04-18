"""Audio recording module — records microphone input to a temp WAV file.

Optionally forces the built-in microphone to avoid Bluetooth SCO degradation.
Also publishes real-time RMS levels for visual waveform.
"""

import logging
import tempfile
import threading
from typing import List, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1

# Keywords for built-in mic detection
_BUILTIN_KEYWORDS = ("macbook", "built-in", "internal", "mac mini", "imac")


def _find_builtin_mic_index() -> Optional[int]:
    """Return index of built-in microphone, or None if not found."""
    try:
        devices = sd.query_devices()
    except Exception as e:
        log.warning("Failed to query audio devices: %s", e)
        return None

    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) < 1:
            continue
        name = dev.get("name", "").lower()
        if any(kw in name for kw in _BUILTIN_KEYWORDS):
            log.info("Using built-in mic: %s (index %d)", dev.get("name"), idx)
            return idx
    log.info("No built-in mic found, using default input")
    return None


class Recorder:
    def __init__(self, force_builtin: bool = True):
        self._frames: List[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._recording = False
        self._force_builtin = force_builtin
        self._current_level = 0.0  # 0..1 RMS level, for overlay
        self._level_callback = None
        self._last_level_time = 0.0  # throttle callback to ~20 fps max

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def current_level(self) -> float:
        """Current audio level (0..1) for UI meters."""
        return self._current_level

    def set_level_callback(self, cb):
        """Register a callback(level: float) called during recording."""
        self._level_callback = cb
        self._persistent_level_callback = cb  # re-attached on each start()

    def start(self) -> None:
        """Start recording audio from the preferred microphone."""
        with self._lock:
            if self._recording:
                return
            self._frames.clear()
            self._recording = True
            self._current_level = 0.0
            self._last_level_time = 0.0
            # Re-attach level callback (stop() detaches it to avoid hangs)
            if hasattr(self, "_persistent_level_callback"):
                self._level_callback = self._persistent_level_callback

            device = _find_builtin_mic_index() if self._force_builtin else None

            try:
                self._stream = sd.InputStream(
                    device=device,
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                    callback=self._audio_callback,
                )
                self._stream.start()
            except Exception as e:
                log.warning("Failed to open device %s, falling back: %s", device, e)
                # Fall back to default device
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                    callback=self._audio_callback,
                )
                self._stream.start()

    def stop(self) -> Optional[str]:
        """Stop recording and return path to the temp WAV file, or None if too short."""
        # Detach the level callback FIRST so audio callback stops posting UI updates.
        # Otherwise stream.stop() can deadlock waiting for pending AppHelper.callAfter's.
        self._level_callback = None

        with self._lock:
            if not self._recording:
                return None
            self._recording = False
            stream = self._stream
            self._stream = None

        # Stop+close OUTSIDE the lock so audio callback (holds no lock) can finish cleanly
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as log_err:
                log.warning("Stream stop error: %s", log_err)

        if not self._frames:
            return None

        audio = np.concatenate(self._frames, axis=0)
        duration = len(audio) / SAMPLE_RATE

        if duration < 0.15:
            return None

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(tmp.name, audio, SAMPLE_RATE, subtype="PCM_16")
        return tmp.name

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            pass
        self._frames.append(indata.copy())

        # Mix RMS (average energy) + peak (max amplitude) for lively meters.
        arr = indata.astype(np.float32)
        rms = float(np.sqrt(np.mean(arr ** 2)))
        peak = float(np.max(np.abs(arr)))
        raw = 0.5 * rms + 0.5 * peak
        level = min(1.0, (raw * 10.0) ** 0.6)

        self._current_level = level

        # Throttle UI level updates to ~30 fps max (one every 33ms).
        # Audio callback fires much faster and flooding AppHelper.callAfter
        # was backing up the main run loop and causing stop() to hang.
        import time as _t
        now = _t.time()
        cb = self._level_callback
        if cb is not None and now - self._last_level_time >= 0.033:
            self._last_level_time = now
            try:
                cb(level)
            except Exception:
                pass
