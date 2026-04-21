"""Sound feedback module — generates quiet start/stop beeps."""

import os
import atexit
import tempfile
import threading
import math
import shlex
import struct
import wave

try:
    from AppKit import NSSound
    _USE_NSSOUND = True
except ImportError:
    _USE_NSSOUND = False

from typing import Dict, Any

_sounds_cache: Dict[str, str] = {}
# Keep a strong reference to each NSSound object. Without this, the
# object goes out of scope after _play_file() returns and Python's GC
# can free it mid-playback — leading to the user not hearing the beep,
# especially after app-switching when CoreAudio has been paused.
_nssound_cache: Dict[str, Any] = {}


def _cleanup_cache() -> None:
    for path in list(_sounds_cache.values()):
        try:
            os.unlink(path)
        except OSError:
            pass
    _sounds_cache.clear()
    _nssound_cache.clear()


atexit.register(_cleanup_cache)


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


def _afplay(path: str) -> None:
    """Reliable fallback: launch /usr/bin/afplay. Apple-signed tool,
    fire-and-forget, survives app switching / CoreAudio context changes."""
    import subprocess
    try:
        subprocess.Popen(
            ["/usr/bin/afplay", "-v", "0.3", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _play_file(path: str) -> None:
    """Play a WAV file asynchronously.

    Strategy: try cached NSSound first (low latency), fall back to
    afplay if NSSound.play() reports failure. NSSound occasionally
    returns False after macOS app-switches briefly pause CoreAudio
    for our process — afplay spawns a fresh subprocess with fresh
    audio-server connection, so it always works.
    """
    played = False
    if _USE_NSSOUND:
        try:
            sound = _nssound_cache.get(path)
            if sound is None:
                sound = NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
                if sound:
                    sound.setVolume_(0.3)
                    _nssound_cache[path] = sound
            if sound:
                # Restart from beginning (prev play may have just ended)
                try:
                    sound.stop()
                except Exception:
                    pass
                played = bool(sound.play())
        except Exception:
            played = False

    if not played:
        _afplay(path)


def play_start() -> None:
    """Play a short high-pitched beep for recording start."""
    path = _generate_tone(frequency=880, duration=0.08, volume=0.15)
    threading.Thread(target=_play_file, args=(path,), daemon=True).start()


def play_stop() -> None:
    """Play a short lower-pitched beep for recording stop."""
    path = _generate_tone(frequency=660, duration=0.08, volume=0.15)
    threading.Thread(target=_play_file, args=(path,), daemon=True).start()
