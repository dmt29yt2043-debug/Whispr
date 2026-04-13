"""Fn/Globe key detection via macOS Quartz CGEventTap.

Supports two modes:
  1. Hold mode: hold Fn to record, release to stop
  2. Toggle mode: double-tap Fn to start, single tap Fn to stop
"""

import time
import threading
import logging
from typing import Callable, Optional

import Quartz
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CFMachPortCreateRunLoopSource,
    CFRunLoopGetCurrent,
    CFRunLoopAddSource,
    CFRunLoopRun,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventTapOptionListenOnly,
    kCGEventFlagsChanged,
    kCFRunLoopCommonModes,
)

log = logging.getLogger(__name__)

# Fn/Globe modifier flag
_FN_FLAG = 0x800000

# Double-tap detection window (seconds)
_DOUBLE_TAP_WINDOW = 0.35

# Minimum hold duration (seconds)
_MIN_HOLD = 0.2


class FnKeyHandler:
    """Detects Fn key press/release and double-tap via CGEventTap."""

    def __init__(self, on_start: Callable, on_stop: Callable):
        self._on_start = on_start
        self._on_stop = on_stop

        self._fn_down = False
        self._fn_down_time = 0.0
        self._last_tap_time = 0.0  # time of last short-tap release
        self._toggle_mode = False
        self._recording = False
        self._thread: Optional[threading.Thread] = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """Start listening for Fn key events in a background thread."""
        self._thread = threading.Thread(target=self._run_event_tap, daemon=True)
        self._thread.start()

    def _run_event_tap(self) -> None:
        mask = 1 << kCGEventFlagsChanged

        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            mask,
            self._event_callback,
            None,
        )

        if tap is None:
            log.error(
                "Failed to create CGEventTap. "
                "Grant Accessibility permission in System Settings > "
                "Privacy & Security > Accessibility."
            )
            return

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        log.info("Fn key event tap started")
        CFRunLoopRun()

    def _event_callback(self, proxy, event_type, event, refcon):
        flags = CGEventGetFlags(event)
        fn_pressed = bool(flags & _FN_FLAG)
        now = time.time()

        if fn_pressed and not self._fn_down:
            self._fn_down = True
            self._fn_down_time = now
            self._on_fn_press(now)

        elif not fn_pressed and self._fn_down:
            self._fn_down = False
            hold_duration = now - self._fn_down_time
            self._on_fn_release(now, hold_duration)

        return event

    def _on_fn_press(self, now: float) -> None:
        # Toggle mode: currently recording → stop
        if self._toggle_mode and self._recording:
            self._toggle_mode = False
            self._recording = False
            self._on_stop()
            return

        # Start recording on every press (hold mode by default)
        self._recording = True
        self._toggle_mode = False
        self._on_start()

    def _on_fn_release(self, now: float, hold_duration: float) -> None:
        # Toggle mode: ignore release, keep recording
        if self._toggle_mode:
            return

        if not self._recording:
            self._last_tap_time = now
            return

        if hold_duration < _MIN_HOLD:
            # Short tap — check for double-tap
            time_since_last_tap = now - self._last_tap_time
            if time_since_last_tap < _DOUBLE_TAP_WINDOW:
                # Double-tap! Enter toggle mode, keep recording
                self._toggle_mode = True
                # Recording already started on press — just keep it going
                self._last_tap_time = 0.0  # reset to avoid triple-tap issues
            else:
                # Single short tap — cancel recording
                self._recording = False
                self._on_stop()  # recorder will return None due to short duration
                self._last_tap_time = now
        else:
            # Normal hold release — stop recording
            self._recording = False
            self._on_stop()
            self._last_tap_time = now
