"""Streaming transcription via OpenAI Realtime API WebSocket.

Lifecycle:

    st = StreamingTranscriber()
    st.start(sample_rate=16000)          # opens WebSocket, sends session config
    st.feed(pcm16_bytes)                 # per audio chunk during recording
    ...
    text = st.commit_and_wait(timeout=3) # signals 'done', waits for final transcript
    st.close()

If anything fails, feed/commit/close are all best-effort. Caller should
keep a local audio buffer so it can fall back to batch transcription if
commit_and_wait returns None.

Pricing note: this uses the Realtime API (gpt-4o-mini-realtime-preview).
Audio input tokens are billed at ~$0.0075/min vs $0.003/min for the
batch transcribe endpoint. Only worth enabling if latency matters.
"""

import base64
import json
import logging
import os
import threading
import time
from typing import Optional

try:
    import websocket  # websocket-client
    _WEBSOCKET_AVAILABLE = True
except ImportError:
    _WEBSOCKET_AVAILABLE = False
    websocket = None  # type: ignore

log = logging.getLogger(__name__)

_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
# GA Realtime API model. gpt-realtime-whisper is natively streaming —
# transcript deltas arrive WHILE audio is being appended, so by the time
# the user releases the key most of the text is already here. This is the
# same architecture Wispr Flow uses (ASR concurrent with speech) and the
# reason their end-to-end latency is sub-second.
_TRANSCRIBE_MODEL = "gpt-realtime-whisper"
_CONNECT_TIMEOUT_SEC = 3.0

# Process-wide kill switch for the Realtime API. We flip this to True
# permanently the first time we see a "beta_api_shape_disabled" error
# (OpenAI sunsetted the Beta endpoint in 2026). Without this flag, every
# dictation re-opens the WS just to discover the API is dead and waits
# out the 6s commit_and_wait timeout — adding 6s of latency per phrase.
# Once flipped, start_async() returns False immediately and app.py falls
# straight through to batch transcription.
_REALTIME_DISABLED_FOR_SESSION = False
_REALTIME_DISABLED_REASON = ""


def _disable_realtime(reason: str) -> None:
    global _REALTIME_DISABLED_FOR_SESSION, _REALTIME_DISABLED_REASON
    if not _REALTIME_DISABLED_FOR_SESSION:
        _REALTIME_DISABLED_FOR_SESSION = True
        _REALTIME_DISABLED_REASON = reason
        log.warning(
            "Realtime API disabled for the rest of this session — %s. "
            "All dictations will use batch transcription.", reason,
        )


class StreamingTranscriber:
    """Wrapper around the Realtime WebSocket. One instance per session.

    start_async() opens the connection on a background thread so the main
    thread (recorder start, overlay, sounds) doesn't block on TCP/TLS
    handshake (typically 200-500ms).

    While the socket is opening, feed() accumulates audio into _batch_buffer.
    Once the socket is ready, the background thread flushes complete 100ms
    batches; subsequent feed() calls send full batches directly. All ws.send()
    calls are serialized through _send_lock — websocket-client is NOT
    thread-safe, and concurrent sends previously corrupted WS frames, causing
    silent audio loss mid-recording.
    """

    def __init__(self):
        self._ws: Optional[websocket.WebSocket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._final_text: Optional[str] = None
        # Streaming deltas for the CURRENT (not yet completed) item.
        # gpt-realtime-whisper emits transcript deltas while audio is
        # still being appended; `completed` then carries the final text
        # for the item and we fold it into _final_text. If `completed`
        # never arrives (timeout), the accumulated deltas are still a
        # usable transcript — better than dropping the tail.
        self._partial_text: str = ""
        self._final_event = threading.Event()
        self._error: Optional[str] = None
        self._closed = False
        self._lock = threading.Lock()
        # _send_lock serializes all ws.send() calls. websocket-client is
        # NOT thread-safe — concurrent sends from feed() (audio callback
        # thread) and the connect-thread flush corrupted WS frames, which
        # OpenAI silently dropped. Net effect: random audio chunks vanished
        # mid-recording, transcript came back truncated.
        self._send_lock = threading.Lock()
        # Accumulate chunks into ~100ms batches — sending 250 tiny messages
        # per second (sounddevice fires ~4ms chunks) hits WS rate/throughput
        # limits and increases the risk of concurrent-send races.
        self._batch_buffer = bytearray()
        # 100ms at 24kHz PCM16 = 4800 bytes
        self._batch_target_bytes = 4800
        # Cap while connecting (50s of audio @ 24kHz PCM16 → ~2.4MB)
        self._max_buffer_bytes = 50 * 24000 * 2
        self._pending_commit = False
        self._ready = threading.Event()
        # Diagnostics: count bytes actually sent to WS so we can compare
        # against the total audio length in logs.
        self._bytes_sent = 0
        self._sample_rate = 24000

    # ── Public API ───────────────────────────────────────────────────

    def start_async(self, sample_rate: int = 24000) -> bool:
        """Kick off WebSocket opening on a background thread (non-blocking).

        Returns True if websocket-client + API key are available (connection
        may still fail later). Returns False to decline streaming immediately,
        in which case the caller should fall back to batch.
        """
        if not _WEBSOCKET_AVAILABLE:
            log.warning("websocket-client not installed — streaming unavailable")
            return False
        if not os.environ.get("OPENAI_API_KEY"):
            log.warning("No OPENAI_API_KEY — streaming unavailable")
            return False
        # Realtime API was disabled earlier in this session (e.g. beta
        # endpoint was sunsetted). Don't waste 6s waiting for a connect
        # that will only fail again — go straight to batch.
        if _REALTIME_DISABLED_FOR_SESSION:
            log.info("Realtime disabled (%s) — skipping streaming",
                     _REALTIME_DISABLED_REASON)
            return False
        try:
            import api_status
            if api_status.is_tripped():
                log.info("API breaker open — skipping streaming session")
                return False
        except Exception:
            pass

        self._sample_rate = sample_rate
        t = threading.Thread(
            target=self._connect_and_configure,
            args=(sample_rate,),
            daemon=True,
        )
        t.start()
        return True

    def _connect_and_configure(self, sample_rate: int) -> None:
        try:
            # GA Realtime API: no OpenAI-Beta header. The old
            # "OpenAI-Beta: realtime=v1" marked the retired beta shape and
            # now yields `beta_api_shape_disabled` errors.
            ws = websocket.create_connection(
                _REALTIME_URL,
                header=[
                    f"Authorization: Bearer {os.environ['OPENAI_API_KEY']}",
                ],
                timeout=_CONNECT_TIMEOUT_SEC,
            )
            ws.settimeout(None)

            # GA session shape: session.update with session.type=transcription.
            # turn_detection is omitted (null) — with gpt-realtime-whisper we
            # commit manually at key release. The model streams transcript
            # DELTAS while audio is appended, so unlike the beta gpt-4o
            # models there is no "only first utterance processed" trap:
            # _handle_event accumulates deltas continuously, and `completed`
            # after our commit finalizes the text.
            # NOTE: `prompt` is not supported by gpt-realtime-whisper in GA.
            config = self._build_session_config()
            # Session-config must also go through the send_lock
            with self._send_lock:
                ws.send(json.dumps(config))

            # Publish ws and capture pending state atomically
            with self._lock:
                if self._closed:
                    try: ws.close()
                    except Exception: pass
                    return
                self._ws = ws
                had_pending_commit = self._pending_commit
                buffered_bytes = len(self._batch_buffer)

            # Flush buffered audio in full-size batches. Held send_lock
            # ensures live feed() calls from the audio thread can't
            # interleave sends and corrupt WS frames.
            self._flush_full_batches(ws)

            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()

            # If caller already signalled commit while we were connecting, do it now
            if had_pending_commit:
                self._flush_remainder_and_commit(ws)

            self._ready.set()
            log.info("Streaming session opened (model=%s, sr=%dHz, buffered=%.2fs audio)",
                     _TRANSCRIBE_MODEL, sample_rate, buffered_bytes / (sample_rate * 2))
        except Exception as e:
            log.warning("Realtime WS connect failed: %s — batch fallback will be used", e)
            self._ready.set()  # unblock any waiters even on failure
            self._closed = True

    def _build_session_config(self) -> dict:
        """GA Realtime session.update payload for a transcription session.

        Kept as a separate method so tests can assert the exact shape
        without opening a socket.
        """
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self._sample_rate,
                        },
                        "transcription": {
                            "model": _TRANSCRIBE_MODEL,
                        },
                        # turn_detection deliberately absent → manual commit
                    },
                },
            },
        }

    # Back-compat alias (callers using old API)
    def start(self, sample_rate: int = 24000) -> bool:
        return self.start_async(sample_rate)

    def feed(self, pcm16_bytes: bytes) -> None:
        """Accumulate audio; send in 100ms batches when WS is open."""
        if self._closed:
            return
        with self._lock:
            self._batch_buffer.extend(pcm16_bytes)
            ws = self._ws
            if ws is None:
                # Still connecting — keep accumulating, drop oldest if huge
                if len(self._batch_buffer) > self._max_buffer_bytes:
                    excess = len(self._batch_buffer) - self._max_buffer_bytes
                    del self._batch_buffer[:excess]
                return
        # WS is open — flush any complete batches. Send under send_lock
        # so audio-callback threads can't interleave with each other or
        # with the connect-thread flush.
        self._flush_full_batches(ws)

    def _flush_full_batches(self, ws) -> None:
        """Send complete _batch_target_bytes chunks. Remainder stays buffered."""
        target = self._batch_target_bytes
        with self._send_lock:
            while True:
                with self._lock:
                    if self._closed or len(self._batch_buffer) < target:
                        return
                    chunk = bytes(self._batch_buffer[:target])
                    del self._batch_buffer[:target]
                try:
                    b64 = base64.b64encode(chunk).decode("ascii")
                    ws.send(json.dumps({
                        "type": "input_audio_buffer.append", "audio": b64
                    }))
                    self._bytes_sent += len(chunk)
                except Exception as e:
                    log.warning("Realtime WS feed failed: %s", e)
                    self._closed = True
                    return

    def _flush_remainder_and_commit(self, ws) -> None:
        """Send any remaining <100ms buffer, then commit. Used at end of recording."""
        with self._send_lock:
            with self._lock:
                tail = bytes(self._batch_buffer)
                self._batch_buffer.clear()
            try:
                if tail:
                    b64 = base64.b64encode(tail).decode("ascii")
                    ws.send(json.dumps({
                        "type": "input_audio_buffer.append", "audio": b64
                    }))
                    self._bytes_sent += len(tail)
                ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                log.info(
                    "WS commit sent. Total audio streamed: %.2fs (%d bytes)",
                    self._bytes_sent / (self._sample_rate * 2),
                    self._bytes_sent,
                )
            except Exception as e:
                log.warning("Realtime WS commit failed: %s", e)
                self._closed = True

    def commit_and_wait(self, timeout: float = 8.0) -> Optional[str]:
        """Commit the audio buffer and wait for the final transcript.

        With server_vad, the API emits one completed event per detected
        phrase. _handle_event accumulates them into _final_text as they
        arrive during recording. When the user releases the key:
          1. We clear _final_event so stale state doesn't trigger early return
          2. Send commit (flushes the pending tail audio as final segment)
          3. Wait up to `timeout` for the server to finish the final segment
          4. Poll 700ms for any extra segments, then return accumulated text
        Returns whatever was accumulated (possibly partial) rather than None
        on timeout — a partial result is better than nothing.
        """
        if self._closed:
            return self._result_text()  # may be None if nothing received

        with self._lock:
            ws = self._ws
            if ws is None:
                self._pending_commit = True

        # Wait for WS to become ready (bounded)
        self._ready.wait(timeout=min(timeout, 2.0))

        with self._lock:
            ws = self._ws
            sent_pending = self._pending_commit and ws is not None

        if ws is None:
            # Connection never opened — return whatever we have
            return self._result_text()

        if not sent_pending:
            # CRITICAL: clear _final_event BEFORE commit, otherwise
            # _final_event is already set from prior segments received
            # during recording, and the wait() below returns instantly —
            # we'd miss the final segment triggered by our manual commit.
            self._final_event.clear()
            self._flush_remainder_and_commit(ws)
            if self._closed:
                return self._result_text()

        # Wait for the post-commit transcript event
        if not self._final_event.wait(timeout=timeout):
            result = self._result_text()
            log.info("No post-commit transcript within %.1fs — returning accumulated text (%d chars)",
                     timeout, len(result or ""))
            return result

        # Poll for additional segments (server can emit several)
        while True:
            with self._lock:
                self._final_event.clear()
            if not self._final_event.wait(timeout=0.7):
                break
            log.info("Additional transcript segment received, continuing to wait")

        return self._result_text()

    def _result_text(self) -> Optional[str]:
        """Finalized text plus any un-finalized delta tail.

        If the server never sent `completed` for the last item (timeout,
        connection drop), the accumulated deltas are still a usable
        transcript — losing the tail is strictly worse.
        """
        with self._lock:
            final = (self._final_text or "").strip()
            tail = self._partial_text.strip()
        combined = (final + " " + tail).strip() if tail else final
        return combined or None

    def close(self) -> None:
        self._safe_close()

    # ── Internals ────────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        while not self._closed and self._ws is not None:
            try:
                raw = self._ws.recv()
            except (websocket.WebSocketConnectionClosedException, OSError) as e:
                log.debug("Realtime WS reader exit: %s", e)
                break
            except Exception as e:
                log.warning("Realtime WS reader error: %s", e)
                break
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except Exception:
                continue
            self._handle_event(evt)

    def _handle_event(self, evt: dict) -> None:
        etype = evt.get("type", "")
        # Deltas arrive many times per second — keep those at DEBUG,
        # log everything else at INFO for diagnostics.
        if etype == "conversation.item.input_audio_transcription.delta":
            log.debug("WS event: %s", etype)
            delta = evt.get("delta") or ""
            if delta:
                with self._lock:
                    self._partial_text += delta
            return
        log.info("WS event: %s", etype)

        # Final transcript events — ACCUMULATE instead of overwriting,
        # because the server may emit one `completed` per segment/item.
        # `completed` supersedes the deltas we collected for that item,
        # so the partial buffer is reset here.
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (evt.get("transcript") or "").strip()
            log.info("WS transcript segment: %r", text[:120])
            with self._lock:
                self._partial_text = ""
                if self._final_text:
                    self._final_text = (self._final_text + " " + text).strip()
                else:
                    self._final_text = text
                self._final_event.set()
        elif etype == "conversation.item.input_audio_transcription.failed":
            err = evt.get("error", {}).get("message", "unknown")
            log.warning("Realtime transcription failed: %s", err)
            with self._lock:
                self._error = err
                self._final_event.set()
            # Trip global breaker on quota errors so the rest of the
            # pipeline (batch transcribe, cleanup) skips the API too.
            try:
                import api_status
                api_status.trip(Exception(err))
            except Exception:
                pass
        elif etype == "error":
            err = evt.get("error", {}).get("message", "unknown")
            err_code = evt.get("error", {}).get("code", "")
            log.warning("Realtime WS error event: %s (full: %s)", err, evt)
            with self._lock:
                self._error = err
                self._closed = True   # don't wait for further events
                self._final_event.set()  # unblock commit_and_wait NOW
            # Beta API was permanently disabled by OpenAI. Flip the
            # session-wide kill switch so subsequent dictations skip
            # streaming entirely instead of paying the 6s timeout each.
            if err_code == "beta_api_shape_disabled" or "Beta API" in err:
                _disable_realtime(f"OpenAI: {err}")
            try:
                import api_status
                api_status.trip(Exception(err))
            except Exception:
                pass

    def _safe_close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
