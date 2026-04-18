"""Secondary hotkey: Cmd+Shift+V — re-paste the last transcription.

Separate CGEventTap listening for keyDown events; triggers when the
user hits Cmd+Shift+V (in case the main Cmd+V after dictation landed
in the wrong place).
"""

import logging
import threading
from typing import Callable

from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CFMachPortCreateRunLoopSource,
    CFRunLoopGetMain,
    CFRunLoopAddSource,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventKeyDown,
    kCFRunLoopCommonModes,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskShift,
)

log = logging.getLogger(__name__)

_TAP_DEFAULT = 0
_kCGKeyboardEventKeycode = 6
_V_KEYCODE = 9


class RePasteHotkey:
    """Listens for Cmd+Shift+V; calls on_trigger() when pressed."""

    def __init__(self, on_trigger: Callable[[], None]):
        self._on_trigger = on_trigger
        self._tap = None
        self._source = None
        self._callback_ref = None

    def start(self) -> bool:
        self._callback_ref = self._event_callback
        mask = 1 << kCGEventKeyDown
        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            _TAP_DEFAULT,
            mask,
            self._callback_ref,
            None,
        )
        if tap is None:
            log.warning("Re-paste CGEventTap failed — need Accessibility")
            return False

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)

        self._tap = tap
        self._source = source
        log.info("Re-paste hotkey installed: Cmd+Shift+V")
        return True

    def _event_callback(self, proxy, event_type, event, refcon):
        try:
            keycode = CGEventGetIntegerValueField(event, _kCGKeyboardEventKeycode)
            if keycode != _V_KEYCODE:
                return event

            flags = CGEventGetFlags(event)
            has_cmd = bool(flags & kCGEventFlagMaskCommand)
            has_shift = bool(flags & kCGEventFlagMaskShift)

            if has_cmd and has_shift:
                log.info("Cmd+Shift+V detected — triggering re-paste")
                # Run callback on a worker thread to not block the tap
                threading.Thread(target=self._on_trigger, daemon=True).start()
                # Suppress the event so no app sees Cmd+Shift+V
                return None

            return event
        except Exception as e:
            log.error("Re-paste callback error: %s", e)
            return event
