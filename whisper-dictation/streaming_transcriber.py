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

import websocket  # websocket-client

log = logging.getLogger(__name__)

_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
_CONNECT_TIMEOUT_SEC = 3.0


class StreamingTranscriber:
    """Synchronous wrapper around the Realtime WebSocket. One instance per session."""

    def __init__(self):
        self._ws: Optional[websocket.WebSocket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._final_text: Optional[str] = None
        self._final_event = threading.Event()
        self._error: Optional[str] = None
        self._closed = False
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────

    def start(self, sample_rate: int = 16000) -> bool:
        """Open WebSocket + configure transcription session. Returns True on success."""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            log.warning("No OPENAI_API_KEY — streaming unavailable")
            return False

        try:
            self._ws = websocket.create_connection(
                _REALTIME_URL,
                header=[
                    f"Authorization: Bearer {api_key}",
                    "OpenAI-Beta: realtime=v1",
                ],
                timeout=_CONNECT_TIMEOUT_SEC,
            )
            # Don't block on recv while streaming
            self._ws.settimeout(None)
        except Exception as e:
            log.warning("Realtime WS connect failed: %s", e)
            self._ws = None
            return False

        # Configure transcription-only session. turn_detection=None means
        # WE control when to commit (on Fn release).
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
        try:
            self._ws.send(json.dumps(config))
        except Exception as e:
            log.warning("Realtime WS session.update failed: %s", e)
            self._safe_close()
            return False

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        log.info("Streaming session opened (model=%s, sr=%dHz)", _TRANSCRIBE_MODEL, sample_rate)
        return True

    def feed(self, pcm16_bytes: bytes) -> None:
        """Send a chunk of PCM16 audio. Non-blocking; errors logged and swallowed."""
        if not self._ws or self._closed:
            return
        try:
            b64 = base64.b64encode(pcm16_bytes).decode("ascii")
            msg = json.dumps({
                "type": "input_audio_buffer.append",
                "audio": b64,
            })
            self._ws.send(msg)
        except Exception as e:
            log.debug("Realtime WS feed failed: %s", e)
            # One-shot: if send fails, mark closed so we don't keep trying
            self._closed = True

    def commit_and_wait(self, timeout: float = 3.0) -> Optional[str]:
        """Send commit + wait for final transcription. Returns None on timeout/failure."""
        if not self._ws or self._closed:
            return None

        try:
            self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
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
