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

# Module-level cache of the last transcription so the re-paste hotkey
# can recover text if the user's cursor was off-target at paste time.
_last_transcription: str = ""


def get_last_transcription() -> str:
    """Return the most recent transcription (may be empty)."""
    return _last_transcription


def set_last_transcription(text: str) -> None:
    global _last_transcription
    _last_transcription = text or ""


def _press_cmd_v() -> None:
    """Simulate Cmd+V via Quartz CGEvents."""
    down = CGEventCreateKeyboardEvent(None, V_KEY_CODE, True)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)

    up = CGEventCreateKeyboardEvent(None, V_KEY_CODE, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, up)


def repaste_last(restore_clipboard: bool = True) -> bool:
    """Paste the last transcription without permanently touching the clipboard.

    Behaviour:
      1. Snapshot the current clipboard.
      2. Put the last transcription on the clipboard, send Cmd+V.
      3. After ~600ms, restore the snapshot — UNLESS the user has copied
         something new in the meantime (we detect by comparing clipboard
         content to what we just put there).

    The 600ms gap exists because some apps read the clipboard asynchronously
    after receiving Cmd+V; restoring too fast can cause them to paste the
    OLD content instead of our text.

    Returns True if there was a transcription to paste.
    """
    if not _last_transcription:
        log.info("Re-paste requested but no previous transcription")
        return False
    text = _last_transcription

    # Snapshot user's existing clipboard so we can put it back.
    prev_clipboard: Optional[str] = None
    if restore_clipboard:
        try:
            prev_clipboard = pyperclip.paste()
        except Exception:
            prev_clipboard = None

    try:
        # Wait up to 350ms for the clipboard manager to actually accept
        # our copy (Alfred/Raycast/Paste.app sometimes hold it briefly).
        # Without this, Cmd+V can paste stale content.
        pyperclip.copy(text)
        deadline = time.time() + 0.30
        while time.time() < deadline:
            try:
                if pyperclip.paste() == text:
                    break
            except Exception:
                break
            time.sleep(0.02)

        _press_cmd_v()
        log.info("Re-pasted last transcription (%d chars)", len(text))

        # Restore the user's clipboard in the background — only if it
        # still contains our text (i.e. the user hasn't copied something
        # new during the wait, in which case we must not clobber their copy).
        if restore_clipboard and prev_clipboard is not None:
            def _restore():
                time.sleep(0.6)
                try:
                    if pyperclip.paste() == text:
                        pyperclip.copy(prev_clipboard)
                        log.debug("Clipboard restored after re-paste")
                    else:
                        log.debug("Clipboard changed by user — skipping restore")
                except Exception:
                    pass
            threading.Thread(target=_restore, daemon=True).start()
        return True
    except Exception as e:
        log.error("Re-paste failed: %s", e)
        return False


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

    # Copy text to clipboard and verify the copy actually succeeded before
    # firing Cmd+V. Clipboard managers (Alfred, Raycast, Paste.app) can
    # intercept pyperclip.copy() and 50ms isn't always enough. If the
    # clipboard doesn't contain our text, wait up to 300ms more.
    pyperclip.copy(text)
    deadline = time.time() + 0.30
    while time.time() < deadline:
        try:
            if pyperclip.paste() == text:
                break
        except Exception:
            break
        time.sleep(0.02)
    else:
        log.warning("Clipboard didn't receive our text within 350ms — paste may paste stale content")

    if can_paste:
        _press_cmd_v()
        log.info("Injected %d chars into focused app", len(text))
        result = "pasted"

        # Restore previous clipboard — but ONLY if the clipboard still
        # contains the text we just injected. If the user copied something
        # new during the 0.6s wait, we must not clobber their copy.
        if restore_clipboard and prev_clipboard is not None:
            injected = text  # capture for closure
            def _restore():
                time.sleep(0.6)
                try:
                    if pyperclip.paste() == injected:
                        pyperclip.copy(prev_clipboard)
                        log.debug("Clipboard restored")
                    else:
                        log.debug("Clipboard was changed by user — skipping restore")
                except Exception:
                    pass
            threading.Thread(target=_restore, daemon=True).start()
    else:
        log.info("No text focus, copied %d chars to clipboard", len(text))
        result = "copied"

    return result
