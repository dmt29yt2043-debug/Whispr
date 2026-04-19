"""Secondary hotkeys via CGEventTap:
  - Cmd+Shift+V — re-paste the last transcription
  - Escape     — emergency cancel: stop any ongoing recording/processing

Separate CGEventTap listening for keyDown events. Runs on the main
CFRunLoop alongside the primary hotkey tap.
"""

import logging
import threading
from typing import Callable, Optional

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
_ESCAPE_KEYCODE = 53


class RePasteHotkey:
    """Listens for Cmd+Shift+V (re-paste) and Escape (cancel)."""

    def __init__(
        self,
        on_trigger: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ):
        self._on_trigger = on_trigger
        self._on_cancel = on_cancel
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
        log.info("Re-paste + cancel hotkeys installed: Cmd+Shift+V / Escape")
        return True

    def _event_callback(self, proxy, event_type, event, refcon):
        try:
            keycode = CGEventGetIntegerValueField(event, _kCGKeyboardEventKeycode)

            # Escape — cancel any ongoing recording/processing.
            # Don't suppress: let the Escape event pass through to the app
            # the user was working in.
            if keycode == _ESCAPE_KEYCODE:
                if self._on_cancel is not None:
                    threading.Thread(target=self._on_cancel, daemon=True).start()
                return event

            if keycode == _V_KEYCODE:
                flags = CGEventGetFlags(event)
                has_cmd = bool(flags & kCGEventFlagMaskCommand)
                has_shift = bool(flags & kCGEventFlagMaskShift)

                if has_cmd and has_shift:
                    log.info("Cmd+Shift+V detected — triggering re-paste")
                    threading.Thread(target=self._on_trigger, daemon=True).start()
                    return None  # suppress

            return event
        except Exception as e:
            log.error("Hotkey callback error: %s", e)
            return event
