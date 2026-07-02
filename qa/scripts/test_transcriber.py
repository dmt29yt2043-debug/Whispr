"""TC_TRANSCRIBE_* — model routing & truncation defense.

Background: gpt-4o-transcribe and gpt-4o-mini-transcribe both have a known
bug where long audio (>10s) returns only the first detected phrase. We
route long audio to whisper-1 instead, and sanity-check short-audio output
for unrealistic chars/sec ratios.
"""
from unittest.mock import patch
from _harness import case, run_all

import transcriber
import api_status


def _patched(api_calls, *, duration: float):
    """Stub _call_openai_transcribe so we can assert routing without a real WAV.

    api_calls = [(expected_model, returned_text_or_None)] in expected order.
    Returns the patch context manager + a list that records every call.
    """
    call_log = []

    def fake_call(client, audio_path, model_name):
        idx = len(call_log)
        call_log.append(model_name)
        if idx >= len(api_calls):
            raise RuntimeError(f"unexpected extra call to {model_name} (idx={idx})")
        expected_model, ret = api_calls[idx]
        assert model_name == expected_model, (
            f"call {idx}: expected {expected_model}, got {model_name}"
        )
        return ret

    ctx = patch.multiple(
        transcriber,
        _get_openai_client=lambda: object(),  # truthy stub
        _audio_duration_seconds=lambda p: duration,
        _call_openai_transcribe=fake_call,
    )
    return ctx, call_log


@case("TC_TRANSCRIBE_SHORT_OK", "transcriber",
      "short audio (≤8s) with realistic chars/sec → returns gpt-4o-transcribe result, no fallback")
def test_short_ok():
    ctx, calls = _patched(
        [("gpt-4o-transcribe", "Hello world this is normal speech.")],
        duration=3.0,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert out == "Hello world this is normal speech.", f"got {out!r}"
    assert calls == ["gpt-4o-transcribe"], f"unexpected fallback: {calls}"


@case("TC_TRANSCRIBE_LONG_USES_WHISPER1", "transcriber",
      "audio >8s skips gpt-4o-* entirely and goes straight to whisper-1")
def test_long_routed_to_whisper1():
    ctx, calls = _patched(
        [("whisper-1", "Длинный текст полностью транскрибирован без обрезок целиком.")],
        duration=15.0,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert "Длинный текст" in out, f"got {out!r}"
    assert calls == ["whisper-1"], f"long audio should bypass gpt-4o: {calls}"


@case("TC_TRANSCRIBE_TRUNCATION_DETECTED", "transcriber",
      "short audio where gpt-4o returns suspiciously few chars/sec → falls back through chain to whisper-1")
def test_truncation_triggers_fallback():
    # 7-second audio, 7 chars returned = 1 c/s. That's truncation (< 6 c/s).
    ctx, calls = _patched(
        [
            ("gpt-4o-transcribe", "Привет."),  # 7/7 = 1.0 c/s — truncated
            ("gpt-4o-mini-transcribe", "Привет, как дела?"),  # 17/7 = 2.4 c/s — still truncated
            ("whisper-1", "Привет, как дела сегодня и что ты делаешь сейчас на работе плюс."),
        ],
        duration=7.0,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert "сегодня и что ты делаешь" in out, f"got {out!r}"
    assert calls == ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"], f"got {calls}"


@case("TC_TRANSCRIBE_VERY_SHORT_NO_TRUNCATION_CHECK", "transcriber",
      "very short audio (<3s) bypasses chars/sec check — short replies are legit")
def test_short_skips_truncation_check():
    # 2 seconds, 3 chars — would fail c/s test if applied; but under 3s we skip the check
    ctx, calls = _patched(
        [("gpt-4o-transcribe", "Да.")],
        duration=2.0,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert out == "Да.", f"got {out!r}"
    assert calls == ["gpt-4o-transcribe"], f"under-3s should not trigger fallback: {calls}"


@case("TC_TRANSCRIBE_LONG_WHISPER1_FAILS_FALLS_TO_MINI", "transcriber",
      "if whisper-1 returns empty on long audio, falls back to gpt-4o-mini-transcribe")
def test_long_whisper_failure_fallback():
    ctx, calls = _patched(
        [
            ("whisper-1", ""),  # failed/empty
            ("gpt-4o-mini-transcribe", "fallback partial result"),
        ],
        duration=12.0,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert out == "fallback partial result", f"got {out!r}"
    assert calls == ["whisper-1", "gpt-4o-mini-transcribe"], f"got {calls}"


@case("TC_TRANSCRIBE_BOUNDARY_8S_USES_GPT4O", "transcriber",
      "exactly 8.0s is NOT 'long' → uses gpt-4o-transcribe")
def test_boundary_eight_seconds():
    ctx, calls = _patched(
        [("gpt-4o-transcribe", "Восемь секунд это короткая запись для тестирования полностью.")],
        duration=8.0,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert "Восемь секунд" in out, f"got {out!r}"
    assert calls == ["gpt-4o-transcribe"], f"got {calls}"


@case("TC_TRANSCRIBE_BREAKER_SKIPS_API", "transcriber",
      "circuit breaker open → _transcribe_api returns None without ANY API calls")
def test_breaker_skips_api_entirely():
    api_status.reset()
    api_status.trip(Exception("insufficient_quota"))
    try:
        # Patch in a stub that, if called, fails the test
        ctx, calls = _patched(
            [],  # no calls expected
            duration=5.0,
        )
        with ctx:
            out = transcriber._transcribe_api("/tmp/fake.wav")
        assert out is None, f"breaker open should return None, got {out!r}"
        assert calls == [], f"breaker open should make zero API calls, got {calls}"
    finally:
        api_status.reset()


@case("TC_TRANSCRIBE_BREAKER_TRIPS_MID_CHAIN", "transcriber",
      "if first model trips breaker, remaining models in fallback chain are skipped")
def test_breaker_trips_mid_chain():
    api_status.reset()

    call_log = []

    def fake_call(client, audio_path, model_name):
        call_log.append(model_name)
        # First call simulates a quota error → trips breaker → returns None
        if model_name == "gpt-4o-transcribe":
            api_status.trip(Exception("insufficient_quota"))
            return None
        # If we ever reach the next models, the test fails
        raise AssertionError(f"unexpected call to {model_name} after breaker trip")

    ctx = patch.multiple(
        transcriber,
        _get_openai_client=lambda: object(),
        _audio_duration_seconds=lambda p: 5.0,
        _call_openai_transcribe=fake_call,
    )
    try:
        with ctx:
            out = transcriber._transcribe_api("/tmp/fake.wav")
        assert out is None
        assert call_log == ["gpt-4o-transcribe"], (
            f"only first model should run before breaker bails, got {call_log}"
        )
    finally:
        api_status.reset()


@case("TC_TRANSCRIBE_REAL_15S_NO_TRUNCATION", "transcriber",
      "regression: 15s audio that we logged returning 73 chars now goes to whisper-1")
def test_regression_15s():
    """Re-creates the May 2 incident: user said 15s of speech, gpt-4o returned 73 chars."""
    full_text = (
        "Судя по всему, ChatGPT заберут travel, всех этих чуваков просто поломают. "
        "А потом они переключатся на ивенты и доставку, потому что это next logical step."
    )
    ctx, calls = _patched(
        [("whisper-1", full_text)],
        duration=15.3,
    )
    with ctx:
        out = transcriber._transcribe_api("/tmp/fake.wav")
    assert out == full_text
    # Critical: NOT calling gpt-4o-transcribe for long audio anymore
    assert "gpt-4o-transcribe" not in calls, (
        f"gpt-4o-transcribe should NOT be called for 15s audio, got {calls}"
    )
    assert calls == ["whisper-1"]


@case("TC_TRANSCRIBE_CLIENT_CACHED", "transcriber",
      "_get_openai_client returns the SAME instance across calls (connection pool reuse)")
def test_client_cached():
    import os
    transcriber._client_cache = None  # reset module state
    old = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "sk-test-fake-key"
    try:
        c1 = transcriber._get_openai_client()
        c2 = transcriber._get_openai_client()
        assert c1 is not None
        assert c1 is c2, "client must be cached — new client per call = new TLS handshake"
    finally:
        transcriber._client_cache = None
        if old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old


@case("TC_TRANSCRIBE_FLAC_UPLOAD_PREP", "transcriber",
      "48kHz WAV is re-encoded to 16kHz mono FLAC, much smaller, same duration")
def test_flac_upload_prep():
    import os
    import tempfile
    import numpy as np
    import soundfile as sf

    # 2 seconds of 48kHz speech-band noise (compressible but non-trivial)
    sr = 48000
    rng = np.random.RandomState(42)
    data = (0.1 * np.sin(2 * np.pi * 220 * np.linspace(0, 2, sr * 2))
            + 0.02 * rng.randn(sr * 2)).astype("float32")
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.close()
    sf.write(wav.name, data, sr, subtype="PCM_16")

    path, is_temp = transcriber._prepare_upload_file(wav.name)
    try:
        assert is_temp, "should have produced a temp FLAC"
        assert path.endswith(".flac")
        info = sf.info(path)
        assert info.samplerate == 16000, f"expected 16kHz, got {info.samplerate}"
        dur = info.frames / info.samplerate
        assert abs(dur - 2.0) < 0.05, f"duration changed: {dur}"
        wav_size = os.path.getsize(wav.name)
        flac_size = os.path.getsize(path)
        assert flac_size < wav_size / 2, (
            f"FLAC not smaller enough: {flac_size} vs {wav_size}"
        )
    finally:
        os.unlink(wav.name)
        if is_temp:
            os.unlink(path)


@case("TC_TRANSCRIBE_UPLOAD_PREP_MISSING_FILE", "transcriber",
      "upload prep on unreadable file falls back to the original path (no crash)")
def test_upload_prep_fallback():
    path, is_temp = transcriber._prepare_upload_file("/tmp/definitely-missing-file.wav")
    assert path == "/tmp/definitely-missing-file.wav"
    assert is_temp is False


if __name__ == "__main__":
    run_all("test_transcriber")
