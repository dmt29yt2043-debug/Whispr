"""Audio recording module — records microphone input to a temp WAV file."""

import tempfile
import threading
from typing import List, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

SAMPLE_RATE = 16000
CHANNELS = 1


class Recorder:
    def __init__(self):
        self._frames: List[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """Start recording audio from the default microphone."""
        with self._lock:
            if self._recording:
                return
            self._frames.clear()
            self._recording = True
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._audio_callback,
            )
            self._stream.start()

    def stop(self) -> Optional[str]:
        """Stop recording and return path to the temp WAV file, or None if too short."""
        with self._lock:
            if not self._recording:
                return None
            self._recording = False
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None

            if not self._frames:
                return None

            audio = np.concatenate(self._frames, axis=0)
            duration = len(audio) / SAMPLE_RATE

            # Ignore recordings shorter than 0.2 seconds
            if duration < 0.2:
                return None

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            sf.write(tmp.name, audio, SAMPLE_RATE)
            return tmp.name

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            pass  # ignore overflow warnings silently
        self._frames.append(indata.copy())
