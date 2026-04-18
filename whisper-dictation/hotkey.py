"""Unified hotkey detection via CGEventTap — no subprocess, no pynput.

Single CGEventTap on the main CFRunLoop catches all modifier flag changes
AND key down/up events for supported hotkeys. This is the same approach
OpenWhispr uses (Swift, .defaultTap mode, main run loop).

Supported keys:
  - Modifier keys via flagsChanged: fn, right_option, left_option,
    right_cmd, left_cmd, right_shift, left_shift, right_ctrl
  - Toggle/function keys via keyDown: caps_lock, f13..f19

Call install() from the MAIN thread BEFORE NSApplication.run().
"""

import time
import logging
import threading
from typing import Callable, Optional, Tuple

import Quartz
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CGEventGetType,
    CGEventGetIntegerValueField,
    CFMachPortCreateRunLoopSource,
    CFRunLoopGetMain,
    CFRunLoopAddSource,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCFRunLoopCommonModes,
)

import settings as S

log = logging.getLogger(__name__)

_TAP_DEFAULT = 0  # kCGEventTapOptionDefault (not listenOnly — we can suppress)
_kCGKeyboardEventKeycode = 6

# ── NSEvent flag masks ───────────────────────────────────────────────
# Main modifier bits
_FLAG_OPTION = 0x80000
_FLAG_COMMAND = 0x100000
_FLAG_SHIFT = 0x20000
_FLAG_CONTROL = 0x40000
_FLAG_FN = 0x800000          # kCGEventFlagMaskSecondaryFn
_FLAG_CAPS_LOCK = 0x10000

# Device-specific bits (left vs right modifiers)
_NX_DEVICELCTLKEYMASK = 0x0001
_NX_DEVICELSHIFTKEYMASK = 0x0002
_NX_DEVICERSHIFTKEYMASK = 0x0004
_NX_DEVICELCMDKEYMASK = 0x0008
_NX_DEVICERCMDKEYMASK = 0x0010
_NX_DEVICELALTKEYMASK = 0x0020
_NX_DEVICERALTKEYMASK = 0x0040
_NX_DEVICERCTLKEYMASK = 0x2000

# ── Key codes (non-modifier keys) ────────────────────────────────────
_KEYCODE_CAPS_LOCK = 57
_KEYCODE_F13 = 105
_KEYCODE_F14 = 107
_KEYCODE_F15 = 113
_KEYCODE_F16 = 106
_KEYCODE_F17 = 64
_KEYCODE_F18 = 79
_KEYCODE_F19 = 80

# ── Timing ───────────────────────────────────────────────────────────
_DOUBLE_TAP_WINDOW = 0.35
_MIN_HOLD = 0.08


def _hotkey_spec(name: str) -> Tuple[str, int, int]:
    """Return (detection_mode, primary_flag, device_flag_or_keycode).

    detection_mode:
      'mod' — flagsChanged, check primary_flag + device_flag
      'fn'  — flagsChanged, check primary_flag only (fn has no device bit)
      'key' — keyDown/keyUp, check keycode
    """
    table = {
        "fn":            ("fn",  _FLAG_FN, 0),
        "right_option":  ("mod", _FLAG_OPTION, _NX_DEVICERALTKEYMASK),
        "left_option":   ("mod", _FLAG_OPTION, _NX_DEVICELALTKEYMASK),
        "right_cmd":     ("mod", _FLAG_COMMAND, _NX_DEVICERCMDKEYMASK),
        "left_cmd":      ("mod", _FLAG_COMMAND, _NX_DEVICELCMDKEYMASK),
        "right_shift":   ("mod", _FLAG_SHIFT, _NX_DEVICERSHIFTKEYMASK),
        "left_shift":    ("mod", _FLAG_SHIFT, _NX_DEVICELSHIFTKEYMASK),
        "right_ctrl":    ("mod", _FLAG_CONTROL, _NX_DEVICERCTLKEYMASK),
        "caps_lock":     ("key", 0, _KEYCODE_CAPS_LOCK),
        "f13":           ("key", 0, _KEYCODE_F13),
        "f14":           ("key", 0, _KEYCODE_F14),
        "f15":           ("key", 0, _KEYCODE_F15),
        "f16":           ("key", 0, _KEYCODE_F16),
        "f17":           ("key", 0, _KEYCODE_F17),
        "f18":           ("key", 0, _KEYCODE_F18),
        "f19":           ("key", 0, _KEYCODE_F19),
    }
    return table.get(name, table["right_option"])


class FnKeyHandler:
    """Unified hotkey handler via CGEventTap.

    Compatible API with the previous multiprocessing-based handler so
    app.py doesn't change.
    """

    def __init__(self, on_start: Callable, on_stop: Callable):
        self._on_start = on_start
        self._on_stop = on_stop

        self._key_down = False
        self._key_down_time = 0.0
        self._last_tap_time = 0.0
        self._toggle_mode = False
        self._recording = False

        # Strong references to native objects
        self._tap = None
        self._source = None
        self._callback_ref = None

        # Read hotkey from settings
        key_name = S.get("hotkey", "right_option")
        self._mode, self._primary_flag, self._device_flag_or_keycode = _hotkey_spec(key_name)
        self._key_name = key_name

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def current_key_name(self) -> str:
        return self._key_name

    def start(self) -> bool:
        """Install the CGEventTap on the main run loop. Returns True on success.

        Must be called from the main thread BEFORE NSApplication.run().
        """
        self._callback_ref = self._event_callback

        # Build the event mask based on hotkey type
        if self._mode in ("mod", "fn"):
            mask = 1 << kCGEventFlagsChanged
        else:  # 'key'
            mask = (1 << kCGEventKeyDown) | (1 << kCGEventKeyUp)

        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            _TAP_DEFAULT,
            mask,
            self._callback_ref,
            None,
        )
        if tap is None:
            log.error(
                "CGEventTapCreate returned None — grant Accessibility permission "
                "to Whisper Dictation.app in System Settings > Privacy & Security > "
                "Accessibility."
            )
            return False

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        if source is None:
            log.error("CFMachPortCreateRunLoopSource returned None")
            return False

        CFRunLoopAddSource(CFRunLoopGetMain(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)

        self._tap = tap
        self._source = source
        log.info("Hotkey tap installed: %s (mode=%s)", self._key_name, self._mode)
        return True

    def restart_with_new_key(self) -> None:
        """Hotkey change requires app restart (can't reinstall a CGEventTap safely)."""
        log.info("Hotkey change will take effect after app restart")

    # ── CGEventTap callback ──────────────────────────────────────────

    def _event_callback(self, proxy, event_type, event, refcon):
        try:
            now = time.time()

            if self._mode in ("mod", "fn"):
                flags = CGEventGetFlags(event)
                if self._mode == "fn":
                    pressed = bool(flags & _FLAG_FN)
                else:  # 'mod' — require both primary and device-specific bits
                    pressed = bool(flags & self._primary_flag) and bool(
                        flags & self._device_flag_or_keycode
                    )
            else:
                # 'key' mode — compare keycode
                keycode = CGEventGetIntegerValueField(event, _kCGKeyboardEventKeycode)
                if keycode != self._device_flag_or_keycode:
                    return event
                etype = CGEventGetType(event)
                if etype == kCGEventKeyDown:
                    pressed = True
                elif etype == kCGEventKeyUp:
                    pressed = False
                else:
                    return event

            # Edge detection
            if pressed and not self._key_down:
                self._key_down = True
                self._key_down_time = now
                self._on_press(now)
                # Suppress the Fn event to prevent emoji picker
                if self._mode == "fn":
                    return None

            elif not pressed and self._key_down:
                self._key_down = False
                hold = now - self._key_down_time
                self._on_release(now, hold)
                if self._mode == "fn":
                    return None

            return event
        except Exception as e:
            log.error("Hotkey callback error: %s", e, exc_info=True)
            return event

    # ── State machine ────────────────────────────────────────────────

    def _on_press(self, now: float) -> None:
        # Toggle mode: currently recording -> stop
        if self._toggle_mode and self._recording:
            self._toggle_mode = False
            self._recording = False
            log.info("Press -> stop (toggle mode)")
            self._call_safe(self._on_stop)
            return

        self._recording = True
        self._toggle_mode = False
        log.info("Press -> start recording")
        self._call_safe(self._on_start)

    def _on_release(self, now: float, hold: float) -> None:
        if self._toggle_mode:
            return

        if not self._recording:
            self._last_tap_time = now
            return

        if hold < _MIN_HOLD:
            time_since_last_tap = now - self._last_tap_time
            if time_since_last_tap < _DOUBLE_TAP_WINDOW:
                # Double tap -> toggle mode (keep recording)
                self._toggle_mode = True
                log.info("Double-tap -> toggle mode ON")
                self._last_tap_time = 0.0
            else:
                self._recording = False
                log.info("Short tap -> cancel")
                self._call_safe(self._on_stop)
                self._last_tap_time = now
        else:
            self._recording = False
            log.info("Release -> stop (hold %.2fs)", hold)
            self._call_safe(self._on_stop)
            self._last_tap_time = now

    def _call_safe(self, fn: Callable) -> None:
        """Run a callback on a worker thread so the event tap callback returns fast."""
        threading.Thread(target=fn, daemon=True).start()
