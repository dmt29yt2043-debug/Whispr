"""Check if the currently focused UI element is a text field (via macOS Accessibility API).

Uses ApplicationServices.AXUIElement to inspect the frontmost app's focused element.
If focus is in a text field / text area / combo box / web area — we can paste.
Otherwise, we just copy to clipboard and let the user paste manually.
"""

import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Editable roles we consider safe to paste into
_TEXT_ROLES = {
    "AXTextField",
    "AXTextArea",
    "AXSearchField",
    "AXComboBox",
    "AXWebArea",       # web pages
    "AXStaticText",    # some apps use editable static text
}


def _get_frontmost_bundle_id() -> Optional[str]:
    """Return the bundle ID of the frontmost application."""
    try:
        from AppKit import NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        if app:
            return str(app.bundleIdentifier() or "")
    except Exception as e:
        log.debug("frontmost bundle check failed: %s", e)
    return None


def get_focused_text_info() -> Tuple[bool, Optional[str]]:
    """Return (has_text_focus, bundle_id).

    has_text_focus: True if frontmost app's focused element is a text-editable role.
    bundle_id: bundle ID of the frontmost app (for per-app settings).
    """
    bundle_id = _get_frontmost_bundle_id()

    try:
        import ApplicationServices as AX
    except ImportError:
        log.debug("ApplicationServices not available")
        return True, bundle_id  # fail open — assume focus is fine

    try:
        # Get the system-wide AX element
        system_wide = AX.AXUIElementCreateSystemWide()

        # Get focused UI element
        err, focused = AX.AXUIElementCopyAttributeValue(
            system_wide, AX.kAXFocusedUIElementAttribute, None
        )
        if err != 0 or focused is None:
            return False, bundle_id

        # Read its role
        err, role = AX.AXUIElementCopyAttributeValue(
            focused, AX.kAXRoleAttribute, None
        )
        if err != 0 or role is None:
            return False, bundle_id

        role_str = str(role)
        is_text = role_str in _TEXT_ROLES

        if not is_text:
            # Check if it has an editable value — some apps use generic roles
            err, value = AX.AXUIElementCopyAttributeValue(
                focused, AX.kAXValueAttribute, None
            )
            if err == 0 and value is not None and isinstance(value, str):
                is_text = True

        log.debug("Focused role: %s → text=%s", role_str, is_text)
        return is_text, bundle_id

    except Exception as e:
        log.debug("AX focus check failed: %s — assuming OK", e)
        return True, bundle_id
