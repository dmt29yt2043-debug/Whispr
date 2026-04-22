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
_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
_CONNECT_TIMEOUT_SEC = 3.0


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
            ws = websocket.create_connection(
                _REALTIME_URL,
                header=[
                    f"Authorization: Bearer {os.environ['OPENAI_API_KEY']}",
                    "OpenAI-Beta: realtime=v1",
                ],
                timeout=_CONNECT_TIMEOUT_SEC,
            )
            ws.settimeout(None)

            # Bilingual prompt helps the model on Russian words — accuracy
            # dropped noticeably without it. anti_hallucination.py catches
            # the rare silent-audio echo via dominant-substring detection.
            config = {
                "type": "transcription_session.update",
                "session": {
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {
                        "model": _TRANSCRIBE_MODEL,
                        "prompt": "Russian and English speech. Русская и английская речь.",
                    },
                    "turn_detection": None,
                },
            }
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

    def commit_and_wait(self, timeout: float = 3.0) -> Optional[str]:
        """Commit the audio buffer and wait for the final transcript."""
        if self._closed:
            return None

        with self._lock:
            ws = self._ws
            if ws is None:
                # WebSocket didn't open yet. Mark commit pending; the
                # connection thread will send commit as soon as it's ready.
                self._pending_commit = True

        # Wait for WS to become ready (bounded) — then flush remainder + commit
        # if we haven't already pushed it from the connect thread.
        self._ready.wait(timeout=min(timeout, 2.0))

        with self._lock:
            ws = self._ws
            sent_pending = self._pending_commit and ws is not None
        if ws is not None and not sent_pending:
            # Flush the last partial batch (may be <100ms) and commit
            # atomically under the send_lock so nothing interleaves.
            self._flush_remainder_and_commit(ws)
            if self._closed:
                return None

        if not self._final_event.wait(timeout=timeout):
            log.warning("Realtime WS commit timeout (%.1fs)", timeout)
            return None

        # After first completed event, poll briefly for additional segments.
        # OpenAI sometimes splits long audio into multiple transcription
        # items — without this loop we'd drop everything after the first.
        while True:
            with self._lock:
                self._final_event.clear()
            if not self._final_event.wait(timeout=0.4):
                break  # No more segments within 400ms — assume done
            log.info("Additional transcript segment received, continuing to wait")

        return self._final_text

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
        # DEBUG: log every event type so we can diagnose segment splits
        log.info("WS event: %s", etype)

        # Final transcript events — ACCUMULATE instead of overwriting,
        # because the server may split long audio into multiple segments
        # and emit a `.completed` per segment (even with turn_detection=None).
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (evt.get("transcript") or "").strip()
            log.info("WS transcript segment: %r", text[:120])
            with self._lock:
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
        elif etype == "error":
            err = evt.get("error", {}).get("message", "unknown")
            log.warning("Realtime WS error event: %s (full: %s)", err, evt)
            with self._lock:
                self._error = err
                self._final_event.set()  # unblock caller

    def _safe_close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
