"""Sound feedback module — start/stop beeps for dictation.

Uses sounddevice (already a project dependency) so we can explicitly
target the built-in Mac speakers regardless of which device is set as
the system default output. MateView / external monitors often claim
the default output slot but have no speakers — causing silent beeps.

Falls back to afplay on the system default if sounddevice fails.
"""

import logging
import math
import os
import subprocess
import tempfile
import threading
import struct
import wave
from typing import Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# Keywords that identify the built-in Mac output device
_BUILTIN_OUTPUT_KW = ("built-in", "macbook", "mac mini", "imac", "internal speaker")

# Cached device index — found once at first play, then reused
_builtin_output_idx: Optional[int] = None
_builtin_output_checked = False
_builtin_lock = threading.Lock()


def _find_builtin_output() -> Optional[int]:
    """Return sounddevice index of the built-in Mac speakers, or None."""
    try:
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if dev.get("max_output_channels", 0) < 1:
                continue
            name = dev.get("name", "").lower()
            if any(kw in name for kw in _BUILTIN_OUTPUT_KW):
                log.info("Built-in output found: %s (index %d)", dev["name"], i)
                return i
    except Exception as e:
        log.debug("query_devices failed: %s", e)
    return None


def _get_builtin_output() -> Optional[int]:
    """Return cached built-in output index (lazy init)."""
    global _builtin_output_idx, _builtin_output_checked
    with _builtin_lock:
        if not _builtin_output_checked:
            _builtin_output_idx = _find_builtin_output()
            _builtin_output_checked = True
        return _builtin_output_idx


def _tone_array(frequency: float, duration: float, volume: float,
                sample_rate: int = 44100) -> np.ndarray:
    """Generate a sine tone with fade-in/out as float32 array."""
    n = int(sample_rate * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    fade = int(0.01 * sample_rate)  # 10ms fade
    env = np.ones(n, dtype=np.float32)
    env[:fade] = np.linspace(0, 1, fade)
    env[n - fade:] = np.linspace(1, 0, fade)
    return (env * volume * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def _play_sd(samples: np.ndarray, sample_rate: int = 44100) -> bool:
    """Play samples via sounddevice on the built-in output. Returns True on success."""
    try:
        device = _get_builtin_output()
        sd.play(samples, samplerate=sample_rate, device=device, blocking=True)
        return True
    except Exception as e:
        log.debug("sounddevice playback failed (device=%s): %s", _get_builtin_output(), e)
        return False


# ── afplay fallback (WAV file, system default device) ────────────────

_wav_cache: dict = {}


def _generate_wav(frequency: float, duration: float, volume: float,
                  sample_rate: int = 44100) -> str:
    """Generate / cache a WAV file for the afplay fallback."""
    key = f"{frequency}_{duration}_{volume}"
    if key in _wav_cache:
        return _wav_cache[key]
    n = int(sample_rate * duration)
    fade = int(0.01 * sample_rate)
    samples = []
    for i in range(n):
        t = i / sample_rate
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        samples.append(int(volume * env * math.sin(2 * math.pi * frequency * t) * 32767))
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="whisper_snd_")
    with wave.open(tmp.name, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{n}h", *samples))
    _wav_cache[key] = tmp.name
    return tmp.name


def _afplay(path: str) -> None:
    try:
        subprocess.Popen(
            ["/usr/bin/afplay", "-v", "1.0", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.debug("afplay failed: %s", e)


# ── Public API ────────────────────────────────────────────────────────

def _play_beep(frequency: float, duration: float, volume: float) -> None:
    """Play a beep: sounddevice on built-in speakers, afplay as fallback."""
    samples = _tone_array(frequency, duration, volume)
    if not _play_sd(samples):
        path = _generate_wav(frequency, duration, volume)
        _afplay(path)


def play_start() -> None:
    """High-pitched beep — recording started."""
    threading.Thread(
        target=_play_beep, args=(880, 0.12, 0.7), daemon=True
    ).start()


def play_stop() -> None:
    """Lower-pitched beep — recording stopped."""
    threading.Thread(
        target=_play_beep, args=(660, 0.12, 0.7), daemon=True
    ).start()
