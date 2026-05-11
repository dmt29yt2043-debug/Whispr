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

# MacBook Air and most Mac mics only support 48kHz natively. We record at
# 48kHz and downsample 2:1 → 24kHz for the OpenAI Realtime stream (which
# requires 24kHz for pcm16). WAV files stay at 48kHz — batch transcription
# accepts any sample rate.
SAMPLE_RATE = 48000
STREAM_SAMPLE_RATE = 24000
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
        # BUG FIX #21: separate op_lock serializes whole start/stop
        # operations so two callbacks can't interleave partial state
        # transitions (start A running while stop A in flight).
        self._op_lock = threading.Lock()
        self._recording = False
        self._force_builtin = force_builtin
        self._current_level = 0.0  # 0..1 RMS level, for overlay
        self._level_callback = None
        self._last_level_time = 0.0  # throttle callback to ~20 fps max
        self._last_error = None  # 'mic_silent' | 'mic_denied' | None
        # Optional raw-chunk callback for streaming transcription
        # (fired with PCM16 bytes on every audio callback).
        self._chunk_callback = None
        # Subsystem-dirty flag. Set when (a) macOS wakes from sleep, or
        # (b) a previous recording came back fully silent (peak=rms=0.0).
        # Both indicate PortAudio is holding a stale CoreAudio handle —
        # InputStream opens without error and the callback fires, but the
        # device delivers only zeros. The fix is to terminate+reinitialize
        # PortAudio before opening the next stream. Done lazily inside
        # start() rather than reactively in the wake handler so we don't
        # race with an in-flight recording.
        self._subsystem_dirty = False

    def mark_subsystem_dirty(self, reason: str = "external") -> None:
        """External signal: force PortAudio reinit on next start().

        Called from the macOS wake-from-sleep observer in app.py. Cheap
        no-op if a recording is already in progress (we'll reinit on the
        FOLLOWING start) — re-initing while a stream is open would crash
        the CoreAudio thread.

        ALSO does a proactive background warm-up: reinit + a 50ms dummy
        InputStream open/close. Without the warm-up, the *first* recording
        after wake reliably failed with PaErrorCode -9986 ("Internal
        PortAudio error") because the HAL doesn't fully come back until
        someone opens an InputStream. We do that work now, before the user
        presses Fn, so the visible Fn-press path stays fast and never sees
        the cold-start error.
        """
        log.info("Audio subsystem marked dirty (reason=%s)", reason)
        self._subsystem_dirty = True

        # Skip warm-up if a recording is in flight — opening a parallel
        # stream while another is active deadlocks CoreAudio on some macs.
        if self._recording:
            log.info("Recording in progress, deferring warm-up to next start")
            return

        def _warm_up_async():
            try:
                self._reinit_portaudio()
                # Open + immediately close a tiny stream to force HAL
                # to actually bind to the device. This is what's missing
                # from a bare _terminate()/_initialize() pair.
                probe = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                )
                probe.start()
                probe.stop()
                probe.close()
                self._subsystem_dirty = False
                log.info("PortAudio warmed up after %s — ready", reason)
            except Exception as e:
                # Warm-up failed: leave the dirty flag set so start()
                # will try again (with its retry loop).
                log.warning("PortAudio warm-up failed (%s) — will retry on next start", e)

        threading.Thread(target=_warm_up_async, daemon=True).start()

    @staticmethod
    def _reinit_portaudio() -> None:
        """Force PortAudio to drop and reopen its CoreAudio connections.

        After macOS wake, sounddevice's cached HAL handles point to dead
        Audio Unit instances. _terminate()+_initialize() forces the
        underlying portaudio library to call Pa_Terminate / Pa_Initialize,
        which re-enumerates devices and rebuilds the HAL bridge. Cheap —
        ~50ms — and idempotent.
        """
        try:
            sd._terminate()
            sd._initialize()
            log.info("PortAudio reinitialized")
        except Exception as e:
            # Don't crash the recorder if reinit fails; the open-stream
            # call below will surface a clearer error.
            log.warning("PortAudio reinit failed: %s", e)

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

    def set_chunk_callback(self, cb):
        """Register a callback(pcm16_bytes: bytes) fired with each audio chunk.

        Used by streaming_transcriber to feed the WebSocket. The callback
        must be fast and non-blocking — it runs on the CoreAudio thread.
        """
        self._chunk_callback = cb

    def start(self) -> None:
        """Start recording audio from the preferred microphone.

        Fully serialized with stop() via _op_lock — if another start/stop
        is still running, this waits for it to finish before proceeding.
        """
        self._op_lock.acquire()
        try:
            self._start_locked()
        finally:
            self._op_lock.release()

    def _start_locked(self) -> None:
        # If subsystem was marked dirty (wake-from-sleep, prior silent
        # capture), reinit PortAudio BEFORE acquiring the recording state
        # lock. This is safe: nothing is recording yet so no stream is in
        # flight. We do it outside the with-block because _terminate() can
        # take ~50ms.
        if self._subsystem_dirty:
            self._reinit_portaudio()
            self._subsystem_dirty = False

        with self._lock:
            if self._recording:
                return
            self._frames = []  # fresh list — avoid races with stop()'s concat
            self._recording = True
            self._current_level = 0.0
            self._last_level_time = 0.0
            if hasattr(self, "_persistent_level_callback"):
                self._level_callback = self._persistent_level_callback

            device = _find_builtin_mic_index() if self._force_builtin else None

            # Auto-heal loop: if we get the classic post-wake -9986
            # ("Internal PortAudio error"), reinit and try again. Without
            # this, the user has to quit & relaunch the app — a known
            # complaint after Mac sleeps. We give up after 2 retries to
            # avoid infinite loops if the mic is genuinely broken.
            self._stream = self._open_input_with_retry(device, max_retries=2)
            if self._stream is None:
                self._recording = False
                raise RuntimeError(
                    "Could not open microphone after retries — see prior log"
                )

    def _open_input_with_retry(self, device, max_retries: int = 2):
        """Try to open an InputStream, reinitializing PortAudio on failure.

        Returns the started stream, or None if all attempts fail.
        """
        def _try_open(dev):
            stream = sd.InputStream(
                device=dev,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._audio_callback,
            )
            stream.start()
            return stream

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                return _try_open(device)
            except Exception as e:
                last_err = e
                log.warning(
                    "InputStream open failed on attempt %d/%d (device=%s): %s",
                    attempt + 1, max_retries + 1, device, e,
                )
                # On the second-to-last attempt, also try default device
                # (covers the case where the named built-in mic index is stale).
                if attempt == max_retries - 1 and device is not None:
                    try:
                        log.info("Trying default device after named-device failure")
                        return _try_open(None)
                    except Exception as e2:
                        log.warning("Default device also failed: %s", e2)
                        last_err = e2

                # Don't reinit on the final loop — caller will give up.
                if attempt < max_retries:
                    log.info("Reinitializing PortAudio before retry...")
                    self._reinit_portaudio()
                    # Tiny breather: HAL needs a moment to settle after
                    # _terminate() before a fresh InputStream succeeds.
                    import time as _t
                    _t.sleep(0.1)

        log.error("InputStream open failed after %d attempts. Last error: %s",
                  max_retries + 1, last_err)
        return None

    def stop(self) -> Optional[str]:
        """Stop recording and return path to the temp WAV file, or None if too short."""
        with self._op_lock:
            return self._stop_locked()

    def _stop_locked(self) -> Optional[str]:
        with self._lock:
            if not self._recording:
                return None
            # BUG FIX #5: detach level callback UNDER the lock so the audio
            # callback (which reads self._level_callback under the same lock)
            # can't fire one more time between our detach and stream.stop().
            self._level_callback = None
            self._recording = False
            stream = self._stream
            self._stream = None
            # Snapshot the frames list and replace with a new empty one.
            # This way if a concurrent start() comes in, it gets a fresh list
            # and won't race with our concat() below.
            frames = self._frames
            self._frames = []

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as log_err:
                log.warning("Stream stop error: %s", log_err)

        if not frames:
            return None

        try:
            audio = np.concatenate(frames, axis=0)
        except Exception as e:
            log.warning("Frame concat failed: %s", e)
            return None

        duration = len(audio) / SAMPLE_RATE
        if duration < 0.4:
            # Too short to contain real speech — ignore accidental presses.
            log.info("Recording too short (%.2fs < 0.4s) — ignored", duration)
            return None

        # Check audio amplitude. If the recording is silent, short-circuit
        # the pipeline — don't waste time/money sending silence to Whisper
        # (which hallucinates "Thank you very much" / "You" on empty audio).
        try:
            arr = audio.flatten() if audio.ndim > 1 else audio
            peak = float(np.max(np.abs(arr)))
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            log.info("Audio captured: %.2fs  peak=%.3f  rms=%.4f", duration, peak, rms)
            # BUG FIX #30: only flag as silent/broken mic if the recording
            # is ABSOLUTELY silent (peak < 0.0005 AND rms ≈ 0). A user
            # whispering softly can produce peak ≈ 0.003, which earlier
            # triggered false 'mic muted' errors.
            if peak < 0.0005 and rms < 0.0001:
                log.warning(
                    "SILENT recording (peak=%.5f rms=%.5f). Likely PortAudio "
                    "holding a stale CoreAudio handle (common after macOS wake). "
                    "Marking subsystem dirty — next start() will reinitialize.",
                    peak, rms,
                )
                self._last_error = "mic_silent"
                # Self-healing: the NEXT start() will reinit PortAudio and
                # the recording should work. We don't reinit right now
                # because that's wasted work if the user gives up; we want
                # the cost amortized only when they actually retry.
                self._subsystem_dirty = True
                return None
            # BUG FIX #32: short presses (< 2s) where the audio is just room
            # tone — peak ~0.03, rms ~0.005 — make Whisper hallucinate single
            # words like "Hello", "Ari!", "Bye" or non-Latin glyphs. Real
            # speech in 2s typically produces rms > 0.01. Drop these silently
            # rather than pasting garbage. Longer recordings (≥2s) skip this
            # gate so a softly-whispered long sentence still goes through.
            if duration < 2.0 and rms < 0.008 and peak < 0.06:
                log.info(
                    "Near-silent short press (%.2fs peak=%.3f rms=%.4f) — "
                    "skipping to avoid hallucination",
                    duration, peak, rms,
                )
                self._last_error = None
                return None
            self._last_error = None
        except Exception:
            pass

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            sf.write(tmp.name, audio, SAMPLE_RATE, subtype="PCM_16")
            return tmp.name
        except Exception as e:
            log.error("Failed to write WAV: %s", e)
            return None

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            pass
        # Hold the lock so we don't append to a frames list that's being
        # concatenated in stop(), or being cleared by a concurrent start().
        # Tryacquire (non-blocking) — if we can't get the lock immediately
        # we drop this audio chunk rather than stall the CoreAudio callback.
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._frames.append(indata.copy())
        finally:
            self._lock.release()

        # Mix RMS (average energy) + peak (max amplitude) for lively meters.
        arr = indata.astype(np.float32)
        rms = float(np.sqrt(np.mean(arr ** 2)))
        peak = float(np.max(np.abs(arr)))
        raw = 0.5 * rms + 0.5 * peak
        level = min(1.0, (raw * 10.0) ** 0.6)

        self._current_level = level

        # Throttle UI level updates to ~30 fps max (one every 33ms).
        import time as _t
        now = _t.time()
        cb = self._level_callback
        if cb is not None and now - self._last_level_time >= 0.033:
            self._last_level_time = now
            try:
                cb(level)
            except Exception:
                pass

        # Forward PCM16 bytes to streaming transcriber if attached.
        # Downsample 48kHz → 24kHz (2:1) for OpenAI Realtime API which
        # requires 24kHz pcm16. Simple decimation is fine for speech —
        # anti-alias filter not critical at this ratio for voice band.
        chunk_cb = self._chunk_callback
        if chunk_cb is not None:
            try:
                if SAMPLE_RATE == 2 * STREAM_SAMPLE_RATE:
                    down = arr[::2] if arr.ndim == 1 else arr[::2, 0]
                else:
                    down = arr.flatten() if arr.ndim > 1 else arr
                pcm16 = (np.clip(down, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                chunk_cb(pcm16)
            except Exception as e:
                # Swallow to keep the CoreAudio callback alive; streaming
                # will fall back to batch on final commit.
                log.debug("chunk_callback error: %s", e)
