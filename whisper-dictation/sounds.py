"""Sound feedback module — generates quiet start/stop beeps."""

import os
import tempfile
import threading
import math
import struct
import wave

# Try to use macOS native sound playback
try:
    from AppKit import NSSound
    _USE_NSSOUND = True
except ImportError:
    _USE_NSSOUND = False

from typing import Dict

_sounds_cache: Dict[str, str] = {}


def _generate_tone(frequency: float, duration: float, volume: float, sample_rate: int = 44100) -> str:
    """Generate a short tone WAV file and return its path."""
    cache_key = f"{frequency}_{duration}_{volume}"
    if cache_key in _sounds_cache:
        return _sounds_cache[cache_key]

    n_samples = int(sample_rate * duration)
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        # Apply fade in/out to avoid clicks (10ms fade)
        fade_samples = int(0.01 * sample_rate)
        envelope = 1.0
        if i < fade_samples:
            envelope = i / fade_samples
        elif i > n_samples - fade_samples:
            envelope = (n_samples - i) / fade_samples

        value = volume * envelope * math.sin(2 * math.pi * frequency * t)
        samples.append(int(value * 32767))

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="whisper_snd_")
    with wave.open(tmp.name, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    _sounds_cache[cache_key] = tmp.name
    return tmp.name


def _play_file(path: str) -> None:
    """Play a WAV file asynchronously."""
    if _USE_NSSOUND:
        sound = NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
        if sound:
            sound.setVolume_(0.3)  # quiet
            sound.play()
    else:
        # Fallback: use afplay (macOS built-in)
        os.system(f'afplay -v 0.3 "{path}" &')


def play_start() -> None:
    """Play a short high-pitched beep for recording start."""
    path = _generate_tone(frequency=880, duration=0.08, volume=0.15)
    threading.Thread(target=_play_file, args=(path,), daemon=True).start()


def play_stop() -> None:
    """Play a short lower-pitched beep for recording stop."""
    path = _generate_tone(frequency=660, duration=0.08, volume=0.15)
    threading.Thread(target=_play_file, args=(path,), daemon=True).start()
