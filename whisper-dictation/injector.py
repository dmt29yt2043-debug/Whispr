"""Text injection module — copies text to clipboard and pastes via Cmd+V."""

import time
import logging

import pyperclip
import Quartz
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventSetFlags,
    CGEventPost,
    kCGHIDEventTap,
    kCGEventFlagMaskCommand,
)

log = logging.getLogger(__name__)

# Key code for 'V' on macOS
V_KEY_CODE = 9


def inject_text(text: str) -> None:
    """Copy text to clipboard and simulate Cmd+V to paste into the focused app."""
    if not text:
        return

    pyperclip.copy(text)
    time.sleep(0.05)  # small delay to ensure clipboard is ready

    # Simulate Cmd+V keypress
    _press_cmd_v()
    log.info("Injected %d chars into focused app", len(text))


def _press_cmd_v() -> None:
    """Simulate Cmd+V using Quartz CGEvents."""
    # Key down
    event_down = CGEventCreateKeyboardEvent(None, V_KEY_CODE, True)
    CGEventSetFlags(event_down, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, event_down)

    # Key up
    event_up = CGEventCreateKeyboardEvent(None, V_KEY_CODE, False)
    CGEventSetFlags(event_up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, event_up)
