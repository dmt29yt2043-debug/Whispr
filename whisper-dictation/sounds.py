"""Sound feedback module — start/stop beeps for dictation.

Plays via NSSound on the AppKit main thread (reliable CoreAudio access).
Falls back to afplay subprocess if NSSound fails.

Key requirements:
- NSSound MUST run on the main thread (background-thread calls are silently
  dropped by CoreAudio on macOS 13+).
- Strong reference must be kept in _nssound_cache to prevent GC mid-playback.
"""

import os
import atexit
import subprocess
import tempfile
import threading
import math
import struct
import wave
import logging

log = logging.getLogger(__name__)

try:
    from AppKit import NSSound
    from PyObjCTools import AppHelper
    _USE_NSSOUND = True
except ImportError:
    _USE_NSSOUND = False

from typing import Dict, Any

_sounds_cache: Dict[str, str] = {}
# Strong references prevent GC while NSSound is playing.
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


def _generate_tone(frequency: float, duration: float, volume: float,
                   sample_rate: int = 44100) -> str:
    """Generate a WAV tone and return its cached path."""
    cache_key = f"{frequency}_{duration}_{volume}"
    if cache_key in _sounds_cache:
        return _sounds_cache[cache_key]

    n_samples = int(sample_rate * duration)
    fade = int(0.01 * sample_rate)  # 10ms fade in/out
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n_samples - fade:
            env = (n_samples - i) / fade
        samples.append(int(volume * env * math.sin(2 * math.pi * frequency * t) * 32767))

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="whisper_snd_")
    with wave.open(tmp.name, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{n_samples}h", *samples))

    _sounds_cache[cache_key] = tmp.name
    return tmp.name


def _afplay(path: str) -> None:
    """Fire-and-forget subprocess fallback."""
    try:
        subprocess.Popen(
            ["/usr/bin/afplay", "-v", "1.0", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.debug("afplay failed: %s", e)


def _play_on_main(path: str) -> None:
    """Play WAV via NSSound on the AppKit main thread.

    NSSound from a background thread is silently dropped by CoreAudio on
    macOS 13+ — AppHelper.callAfter guarantees main-thread execution.
    """
    def _do() -> None:
        played = False
        if _USE_NSSOUND:
            try:
                sound = _nssound_cache.get(path)
                if sound is None:
                    sound = NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
                    if sound:
                        sound.setVolume_(1.0)
                        _nssound_cache[path] = sound
                if sound:
                    sound.stop()  # rewind if previously played
                    played = bool(sound.play())
                    if not played:
                        log.debug("NSSound.play() returned False — using afplay fallback")
            except Exception as e:
                log.debug("NSSound error: %s", e)

        if not played:
            _afplay(path)

    if _USE_NSSOUND:
        try:
            AppHelper.callAfter(_do)
            return
        except Exception as e:
            log.debug("callAfter failed: %s — playing directly", e)

    # If callAfter unavailable (e.g. run loop not started yet), play inline
    _afplay(path)


def play_start() -> None:
    """High-pitched beep — recording started."""
    path = _generate_tone(frequency=880, duration=0.12, volume=0.8)
    threading.Thread(target=_play_on_main, args=(path,), daemon=True).start()


def play_stop() -> None:
    """Lower-pitched beep — recording stopped."""
    path = _generate_tone(frequency=660, duration=0.12, volume=0.8)
    threading.Thread(target=_play_on_main, args=(path,), daemon=True).start()
