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
_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
_CONNECT_TIMEOUT_SEC = 3.0


class StreamingTranscriber:
    """Wrapper around the Realtime WebSocket. One instance per session.

    start_async() opens the connection on a background thread so the main
    thread (recorder start, overlay, sounds) doesn't block on TCP/TLS
    handshake (typically 200-500ms).

    While the socket is opening, feed() buffers chunks into _pending_chunks.
    Once the socket is ready, the background thread flushes the buffer and
    subsequent feed() calls go directly.
    """

    def __init__(self):
        self._ws: Optional[websocket.WebSocket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._final_text: Optional[str] = None
        self._final_event = threading.Event()
        self._error: Optional[str] = None
        self._closed = False
        self._lock = threading.Lock()
        self._pending_chunks: list = []
        self._pending_commit = False
        self._ready = threading.Event()

    # ── Public API ───────────────────────────────────────────────────

    def start_async(self, sample_rate: int = 16000) -> bool:
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

            # No prompt — avoids prompt-echo hallucination on short/silent
            # audio. gpt-4o-mini-transcribe is multilingual and handles
            # English+Russian without a hint.
            config = {
                "type": "transcription_session.update",
                "session": {
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {
                        "model": _TRANSCRIBE_MODEL,
                    },
                    "turn_detection": None,
                },
            }
            ws.send(json.dumps(config))

            # Flush any chunks buffered while we were connecting
            with self._lock:
                if self._closed:
                    try: ws.close()
                    except Exception: pass
                    return
                self._ws = ws
                pending = self._pending_chunks
                self._pending_chunks = []
                had_pending_commit = self._pending_commit

            for chunk in pending:
                try:
                    b64 = base64.b64encode(chunk).decode("ascii")
                    ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                except Exception as e:
                    log.debug("Failed to flush buffered chunk: %s", e)
                    break

            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()

            # If caller already signalled commit while we were connecting, do it now
            if had_pending_commit:
                try:
                    ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                except Exception:
                    pass

            self._ready.set()
            log.info("Streaming session opened (model=%s, sr=%dHz, buffered=%d chunks)",
                     _TRANSCRIBE_MODEL, sample_rate, len(pending))
        except Exception as e:
            log.warning("Realtime WS connect failed: %s — batch fallback will be used", e)
            self._ready.set()  # unblock any waiters even on failure
            self._closed = True

    # Back-compat alias (callers using old API)
    def start(self, sample_rate: int = 16000) -> bool:
        return self.start_async(sample_rate)

    def feed(self, pcm16_bytes: bytes) -> None:
        """Send a chunk. If WS not ready yet, buffer it."""
        if self._closed:
            return
        with self._lock:
            ws = self._ws
            if ws is None:
                # Still connecting — buffer (cap to avoid unbounded memory)
                if len(self._pending_chunks) < 500:  # ~10 sec @ 50ms chunks
                    self._pending_chunks.append(pcm16_bytes)
                return
        try:
            b64 = base64.b64encode(pcm16_bytes).decode("ascii")
            ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
        except Exception as e:
            log.debug("Realtime WS feed failed: %s", e)
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

        # Wait for WS to become ready (bounded) — then send commit if we
        # haven't already pushed it from the connect thread.
        self._ready.wait(timeout=min(timeout, 2.0))

        with self._lock:
            ws = self._ws
            sent_pending = self._pending_commit and ws is not None
        if ws is not None and not sent_pending:
            try:
                ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            except Exception as e:
                log.warning("Realtime WS commit failed: %s", e)
                return None

        if self._final_event.wait(timeout=timeout):
            return self._final_text
        log.warning("Realtime WS commit timeout (%.1fs)", timeout)
        return None

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
        # Final transcript events
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (evt.get("transcript") or "").strip()
            with self._lock:
                self._final_text = text
                self._final_event.set()
        elif etype == "conversation.item.input_audio_transcription.failed":
            err = evt.get("error", {}).get("message", "unknown")
            log.warning("Realtime transcription failed: %s", err)
            with self._lock:
                self._final_text = None
                self._error = err
                self._final_event.set()
        elif etype == "error":
            err = evt.get("error", {}).get("message", "unknown")
            log.warning("Realtime WS error event: %s", err)
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
