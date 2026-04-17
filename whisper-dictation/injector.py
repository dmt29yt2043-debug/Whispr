"""Text injection — copies text to clipboard and pastes via Cmd+V.

Features:
- AX focus check: if focused element isn't a text field, just copy to clipboard
- Clipboard restore: saves previous clipboard content, restores it after paste
"""

import time
import logging
import threading
from typing import Optional

import pyperclip
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventSetFlags,
    CGEventPost,
    kCGHIDEventTap,
    kCGEventFlagMaskCommand,
)

log = logging.getLogger(__name__)

V_KEY_CODE = 9  # 'V' on macOS


def _press_cmd_v() -> None:
    """Simulate Cmd+V via Quartz CGEvents."""
    down = CGEventCreateKeyboardEvent(None, V_KEY_CODE, True)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)

    up = CGEventCreateKeyboardEvent(None, V_KEY_CODE, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, up)


def inject_text(
    text: str,
    check_focus: bool = True,
    restore_clipboard: bool = True,
) -> str:
    """Paste text into the focused app, or copy to clipboard if no text focus.

    Returns "pasted" | "copied" | "skipped".
    """
    if not text:
        return "skipped"

    # Save previous clipboard
    prev_clipboard: Optional[str] = None
    if restore_clipboard:
        try:
            prev_clipboard = pyperclip.paste()
        except Exception:
            prev_clipboard = None

    # Check focus
    can_paste = True
    if check_focus:
        try:
            from focus_check import get_focused_text_info
            has_text_focus, _bundle_id = get_focused_text_info()
            can_paste = has_text_focus
        except Exception as e:
            log.debug("focus check error: %s (allowing paste)", e)

    # Copy text to clipboard
    pyperclip.copy(text)
    time.sleep(0.05)

    if can_paste:
        _press_cmd_v()
        log.info("Injected %d chars into focused app", len(text))
        result = "pasted"

        # Restore previous clipboard after a delay
        if restore_clipboard and prev_clipboard is not None:
            def _restore():
                time.sleep(0.6)
                try:
                    pyperclip.copy(prev_clipboard)
                    log.debug("Clipboard restored")
                except Exception:
                    pass
            threading.Thread(target=_restore, daemon=True).start()
    else:
        log.info("No text focus, copied %d chars to clipboard", len(text))
        result = "copied"

    return result
