"""Fn/Globe key detection via CGEventTap (defaultTap mode).

This is the OpenWhispr approach: install an event tap with .defaultTap
(not .listenOnly) on the main CFRunLoop, check for the Fn flag
(kCGEventFlagMaskSecondaryFn = 0x800000), and suppress the event to
kill the emoji picker / input source switcher.

MUST be run on the main thread before the NSApplication event loop starts.
Returns True if the tap was successfully installed, False otherwise.

If this doesn't receive events on your Mac, Python simply cannot see Fn —
system intercepts it below the user-space event pipeline. In that case
the main app falls back to pynput-based subprocess detection.
"""

import time
import logging
import threading
from typing import Callable, Optional

import Quartz
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CFMachPortCreateRunLoopSource,
    CFRunLoopGetMain,
    CFRunLoopAddSource,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventFlagsChanged,
    kCFRunLoopCommonModes,
)

log = logging.getLogger(__name__)

# The Fn/Globe modifier flag — same value as NSEventModifierFlagFunction
# and Swift's .maskSecondaryFn
_FN_FLAG = 0x800000

_MIN_HOLD = 0.08
_DOUBLE_TAP_WINDOW = 0.35

# kCGEventTapOptionDefault = 0 (not listen-only — can suppress events)
_TAP_DEFAULT = 0


class FnHotkey:
    """Fn/Globe key detection via CGEventTap on main run loop."""

    def __init__(self, on_start: Callable, on_stop: Callable):
        self._on_start = on_start
        self._on_stop = on_stop

        self._fn_down = False
        self._fn_down_time = 0.0
        self._last_tap_time = 0.0
        self._toggle_mode = False
        self._recording = False

        # Strong references to prevent GC of native callback
        self._tap = None
        self._source = None
        self._callback_ref = None

        self._installed = False
        self._seen_fn_event = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def seen_fn_event(self) -> bool:
        return self._seen_fn_event

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> bool:
        """Install the event tap on the main run loop. Returns True on success.

        IMPORTANT: call this from the main thread BEFORE NSApplication.run()
        starts the event loop.
        """
        # Create the callback — keep a strong reference to prevent GC
        self._callback_ref = self._event_callback

        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            _TAP_DEFAULT,  # defaultTap — allows suppressing events via returning None
            1 << kCGEventFlagsChanged,
            self._callback_ref,
            None,
        )

        if tap is None:
            log.error("CGEventTapCreate returned None — grant Accessibility.")
            return False

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        if source is None:
            log.error("CFMachPortCreateRunLoopSource returned None")
            return False

        CFRunLoopAddSource(CFRunLoopGetMain(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)

        self._tap = tap
        self._source = source
        self._installed = True
        log.info("Fn CGEventTap installed on main run loop (defaultTap)")
        return True

    def _event_callback(self, proxy, event_type, event, refcon):
        try:
            flags = CGEventGetFlags(event)
            fn_pressed = bool(flags & _FN_FLAG)
            now = time.time()

            if fn_pressed or self._fn_down:
                self._seen_fn_event = True

            if fn_pressed and not self._fn_down:
                self._fn_down = True
                self._fn_down_time = now
                self._on_fn_press(now)
                # Suppress Fn event to prevent emoji picker
                return None

            elif not fn_pressed and self._fn_down:
                self._fn_down = False
                hold = now - self._fn_down_time
                self._on_fn_release(now, hold)
                return None

            # Not an Fn event — pass through
            return event
        except Exception as e:
            log.error("Fn callback error: %s", e)
            return event

    def _on_fn_press(self, now: float) -> None:
        if self._toggle_mode and self._recording:
            self._toggle_mode = False
            self._recording = False
            log.info("Fn press -> stop (toggle mode)")
            self._call_safe(self._on_stop)
            return

        self._recording = True
        self._toggle_mode = False
        log.info("Fn press -> start recording")
        self._call_safe(self._on_start)

    def _on_fn_release(self, now: float, hold: float) -> None:
        if self._toggle_mode:
            return

        if not self._recording:
            self._last_tap_time = now
            return

        if hold < _MIN_HOLD:
            time_since_last_tap = now - self._last_tap_time
            if time_since_last_tap < _DOUBLE_TAP_WINDOW:
                self._toggle_mode = True
                log.info("Fn double-tap -> toggle mode ON")
                self._last_tap_time = 0.0
            else:
                self._recording = False
                log.info("Fn short tap -> cancel")
                self._call_safe(self._on_stop)
                self._last_tap_time = now
        else:
            self._recording = False
            log.info("Fn release -> stop (hold %.2fs)", hold)
            self._call_safe(self._on_stop)
            self._last_tap_time = now

    def _call_safe(self, fn):
        """Call callback on a worker thread to avoid blocking the event tap."""
        threading.Thread(target=fn, daemon=True).start()
