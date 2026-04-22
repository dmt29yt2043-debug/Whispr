"""Unit tests for the hotkey state machine (push-to-talk + double-tap).

These tests drive FnKeyHandler's state machine directly via `_on_press` and
`_on_release`, bypassing CGEventTap. They verify:

- Push-to-talk: hold >= _MIN_HOLD triggers start + stop
- Short tap alone: triggers start, then stop after _DOUBLE_TAP_WINDOW timeout
- Double-tap: second tap within window → toggle mode, recording continues
- Toggle mode: next press stops recording
- reset_state() cancels the pending double-tap timer
"""
import time
import threading
from _harness import case, run_all

from hotkey import FnKeyHandler, _MIN_HOLD, _DOUBLE_TAP_WINDOW


def _make_handler():
    """Build a handler with call-counting stubs instead of real callbacks."""
    calls = {"start": 0, "stop": 0}

    def on_start():
        calls["start"] += 1

    def on_stop():
        calls["stop"] += 1

    h = FnKeyHandler(on_start=on_start, on_stop=on_stop)
    return h, calls


def _wait_for(cond, timeout: float = 1.5, poll: float = 0.01):
    """Spin-wait until cond() is True or timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(poll)
    return False


# ─────────────────────────── Push-to-talk ───────────────────────────

@case("TC_HK_HOLD", "hotkey", "push-to-talk: hold ≥ _MIN_HOLD → start + stop on release")
def test_push_to_talk():
    h, calls = _make_handler()
    t0 = time.time()
    h._on_press(t0)
    # Hold for clearly longer than _MIN_HOLD (0.20s)
    hold = _MIN_HOLD + 0.3
    h._on_release(t0 + hold, hold)
    # _call_safe dispatches to a thread — wait briefly for callbacks
    assert _wait_for(lambda: calls["start"] == 1 and calls["stop"] == 1), \
        f"expected start=1 stop=1, got {calls}"
    assert not h.is_recording


# ─────────────────────────── Single tap ─────────────────────────────

@case("TC_HK_SINGLE_TAP", "hotkey", "single short tap → start + stop after double-tap timeout")
def test_single_tap_timeout():
    h, calls = _make_handler()
    t0 = time.time()
    h._on_press(t0)
    # Short tap — below _MIN_HOLD
    hold = _MIN_HOLD * 0.3
    h._on_release(t0 + hold, hold)
    # start must fire immediately; stop should wait for the timeout
    assert _wait_for(lambda: calls["start"] == 1, timeout=0.5), "start did not fire"
    assert calls["stop"] == 0, "stop fired too early (before timeout)"
    # Now wait past the double-tap window
    assert _wait_for(lambda: calls["stop"] == 1,
                     timeout=_DOUBLE_TAP_WINDOW + 0.5), "stop never fired"
    assert not h.is_recording


# ─────────────────────────── Double-tap ─────────────────────────────

@case("TC_HK_DOUBLE_TAP", "hotkey", "double-tap: two short taps → toggle mode, recording stays on")
def test_double_tap_enters_toggle_mode():
    h, calls = _make_handler()
    t0 = time.time()
    # First short tap
    h._on_press(t0)
    hold = _MIN_HOLD * 0.3
    h._on_release(t0 + hold, hold)
    # Second tap within window (simulate 100ms gap)
    t1 = t0 + hold + 0.10
    h._on_press(t1)
    # Release (also quick)
    h._on_release(t1 + hold, hold)
    # Now wait past where a single-tap timeout would have fired
    time.sleep(_DOUBLE_TAP_WINDOW + 0.2)
    # start should have fired exactly ONCE (first tap) — second tap did
    # NOT restart recording, it just flipped toggle mode.
    # stop should have fired ZERO times (toggle mode keeps recording).
    assert calls["start"] == 1, f"expected start=1, got {calls}"
    assert calls["stop"] == 0, f"expected stop=0 in toggle mode, got {calls}"
    assert h.is_recording, "should still be recording in toggle mode"
    assert h._toggle_mode is True


@case("TC_HK_TOGGLE_STOP", "hotkey", "in toggle mode, next press stops recording")
def test_toggle_mode_stop_on_press():
    h, calls = _make_handler()
    t0 = time.time()
    # Enter toggle mode via double-tap
    h._on_press(t0)
    h._on_release(t0 + 0.05, 0.05)
    h._on_press(t0 + 0.15)
    h._on_release(t0 + 0.20, 0.05)
    # Wait a beat so timer definitely would have fired if toggle failed
    time.sleep(_DOUBLE_TAP_WINDOW + 0.1)
    assert h._toggle_mode is True and calls["stop"] == 0, \
        f"should be in toggle mode with no stops, got {calls}"
    # Now press → should stop
    h._on_press(t0 + 2.0)
    assert _wait_for(lambda: calls["stop"] == 1), "stop did not fire on toggle-mode press"
    assert not h.is_recording
    assert h._toggle_mode is False


# ─────────────────────── reset_state cancels timer ──────────────────

@case("TC_HK_RESET_CANCELS_TIMER", "hotkey", "reset_state() cancels pending double-tap timer")
def test_reset_cancels_double_tap_timer():
    h, calls = _make_handler()
    t0 = time.time()
    h._on_press(t0)
    h._on_release(t0 + 0.05, 0.05)
    # Timer now armed — reset before it fires
    assert h._waiting_for_second_tap is True
    h.reset_state()
    assert h._waiting_for_second_tap is False
    assert h._double_tap_timer is None
    # Wait past where timer would have fired — stop must NOT have fired again
    time.sleep(_DOUBLE_TAP_WINDOW + 0.2)
    assert calls["stop"] == 0, \
        f"reset_state did not cancel timer — stop fired {calls['stop']} times"


# ─────────────────────── Rapid press debounce ───────────────────────

@case("TC_HK_DEBOUNCE", "hotkey", "rapid repeated press without release is debounced")
def test_debounce_rapid_press():
    h, calls = _make_handler()
    t0 = time.time()
    h._on_press(t0)
    # Same-timestamp second press (e.g. duplicated OS event) should not
    # double-fire start.
    h._on_press(t0 + 0.01)
    # Release long → fires stop
    h._on_release(t0 + 0.5, 0.5)
    assert _wait_for(lambda: calls["stop"] == 1)
    assert calls["start"] == 1, \
        f"debounce failed — expected start=1, got {calls}"


if __name__ == "__main__":
    run_all("test_hotkey")
