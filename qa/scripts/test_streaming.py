"""TC_STREAM_* — GA Realtime streaming transcriber unit tests.

No sockets are opened here: we test the session-config shape and the
event-handling state machine directly. The GA migration points:
  - session.update (not the retired transcription_session.update)
  - gpt-realtime-whisper (natively streaming, deltas during speech)
  - no OpenAI-Beta header, no prompt, no turn_detection (manual commit)
  - delta accumulation so a missed `completed` doesn't lose the tail
"""
from _harness import case, run_all

import streaming_transcriber as st_mod
from streaming_transcriber import StreamingTranscriber


@case("TC_STREAM_GA_CONFIG", "streaming",
      "session config uses GA shape: session.update / type=transcription / gpt-realtime-whisper")
def test_ga_config_shape():
    st = StreamingTranscriber()
    cfg = st._build_session_config()
    assert cfg["type"] == "session.update", f"beta shape leaked: {cfg['type']}"
    session = cfg["session"]
    assert session["type"] == "transcription"
    audio_in = session["audio"]["input"]
    assert audio_in["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_in["transcription"]["model"] == "gpt-realtime-whisper"
    # GA: prompt unsupported for gpt-realtime-whisper; manual commit → no VAD
    assert "prompt" not in audio_in["transcription"]
    assert "turn_detection" not in audio_in


@case("TC_STREAM_DELTA_THEN_COMPLETED", "streaming",
      "deltas accumulate; completed supersedes them into final text")
def test_delta_then_completed():
    st = StreamingTranscriber()
    for d in ("Привет", ", как", " дела?"):
        st._handle_event({
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": d,
        })
    assert st._partial_text == "Привет, как дела?"
    st._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Привет, как дела?",
    })
    assert st._final_text == "Привет, как дела?"
    assert st._partial_text == "", "completed must reset the delta buffer"
    assert st._result_text() == "Привет, как дела?"
    assert st._final_event.is_set()


@case("TC_STREAM_TAIL_WITHOUT_COMPLETED", "streaming",
      "if completed never arrives, accumulated deltas are still returned (no lost tail)")
def test_tail_without_completed():
    st = StreamingTranscriber()
    st._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Первая фраза.",
    })
    # Tail deltas for a second item whose completed is never received
    for d in ("Вторая", " фраза", " без финала"):
        st._handle_event({
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": d,
        })
    assert st._result_text() == "Первая фраза. Вторая фраза без финала"


@case("TC_STREAM_MULTI_SEGMENT_ACCUMULATE", "streaming",
      "multiple completed events accumulate with spaces (long dictations)")
def test_multi_segment():
    st = StreamingTranscriber()
    st._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Раз.",
    })
    st._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Два.",
    })
    st._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Три.",
    })
    assert st._final_text == "Раз. Два. Три."


@case("TC_STREAM_ERROR_UNBLOCKS", "streaming",
      "error event closes the session and unblocks commit_and_wait")
def test_error_unblocks():
    st = StreamingTranscriber()
    st._handle_event({
        "type": "error",
        "error": {"message": "some transient server error", "code": "server_error"},
    })
    assert st._closed is True
    assert st._final_event.is_set()
    # Non-quota error must NOT trip the global API breaker
    import api_status
    assert not api_status.is_tripped()


@case("TC_STREAM_BETA_ERROR_DISABLES_SESSIONWIDE", "streaming",
      "beta_api_shape_disabled error flips the session-wide kill switch")
def test_beta_error_kill_switch():
    # Save/restore module state — other tests must not see the switch on
    prev = st_mod._REALTIME_DISABLED_FOR_SESSION
    prev_reason = st_mod._REALTIME_DISABLED_REASON
    try:
        st_mod._REALTIME_DISABLED_FOR_SESSION = False
        st = StreamingTranscriber()
        st._handle_event({
            "type": "error",
            "error": {"message": "The Realtime Beta API is no longer supported.",
                      "code": "beta_api_shape_disabled"},
        })
        assert st_mod._REALTIME_DISABLED_FOR_SESSION is True
    finally:
        st_mod._REALTIME_DISABLED_FOR_SESSION = prev
        st_mod._REALTIME_DISABLED_REASON = prev_reason


class _FakeWS:
    """Collects JSON messages sent over the fake socket."""
    def __init__(self):
        self.sent = []

    def send(self, data):
        import json as _json
        self.sent.append(_json.loads(data))


def _commit_count(fake_ws):
    return sum(1 for m in fake_ws.sent if m.get("type") == "input_audio_buffer.commit")


@case("TC_STREAM_COMMIT_IDEMPOTENT", "streaming",
      "commit_async twice + commit_and_wait sends exactly ONE commit message")
def test_commit_idempotent():
    st = StreamingTranscriber()
    ws = _FakeWS()
    st._ws = ws
    st._ready.set()

    st.commit_async()
    st.commit_async()  # e.g. recorder callback + safety call
    # Simulate the server's final transcript so commit_and_wait returns
    st._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "готово",
    })
    out = st.commit_and_wait(timeout=1.0)
    assert out == "готово"
    assert _commit_count(ws) == 1, (
        f"expected exactly 1 commit, got {_commit_count(ws)}: {ws.sent}"
    )


@case("TC_STREAM_NO_POST_COMPLETED_POLL", "streaming",
      "commit_and_wait returns immediately after completed (no 0.7s poll tax)")
def test_no_poll_tax():
    import time
    import threading

    st = StreamingTranscriber()
    ws = _FakeWS()
    st._ws = ws
    st._ready.set()

    st.commit_async()

    # Deliver `completed` 100ms after the wait starts
    def _deliver():
        time.sleep(0.1)
        st._handle_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "быстрый ответ",
        })
    threading.Thread(target=_deliver, daemon=True).start()

    t0 = time.time()
    out = st.commit_and_wait(timeout=3.0)
    elapsed = time.time() - t0
    assert out == "быстрый ответ"
    assert elapsed < 0.5, (
        f"commit_and_wait took {elapsed:.2f}s — the 0.7s segment poll is back?"
    )


@case("TC_STREAM_COMMIT_BEFORE_CONNECT_QUEUES", "streaming",
      "commit_async while still connecting queues a pending commit (no crash, no double)")
def test_commit_before_connect():
    st = StreamingTranscriber()
    assert st._ws is None
    st.commit_async()
    assert st._pending_commit is True, "commit must be queued for the connect thread"
    assert st._committed is True
    # A second call must not do anything new
    st.commit_async()
    assert st._pending_commit is True


if __name__ == "__main__":
    run_all("test_streaming")
