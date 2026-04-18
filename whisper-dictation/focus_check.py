"""Check if the currently focused UI element is a text field (via macOS Accessibility API).

Uses ApplicationServices.AXUIElement to inspect the frontmost app's focused element.
If focus is in a text field / text area / combo box / web area — we can paste.
Otherwise, we just copy to clipboard and let the user paste manually.

Web browsers use all kinds of custom roles for text inputs via JS frameworks,
so we always allow paste when the frontmost app is a known browser.
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
    "AXWebArea",        # web pages (often the focused element in browsers)
    "AXStaticText",     # some apps use editable static text
    "AXGroup",          # some frameworks wrap text inputs in a group
    "AXScrollArea",     # code editors
    "AXCell",           # spreadsheets
    "AXRow",
    "AXOutline",
    "AXDocument",
}

# Browser bundle IDs — always allow paste regardless of AX role,
# because web pages use custom roles that AX can't reliably classify.
_BROWSER_BUNDLE_IDS = {
    "com.apple.Safari",
    "com.google.Chrome",
    "com.google.Chrome.canary",
    "com.google.Chrome.beta",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",        # Arc
    "company.thebrowser.dia",            # Dia
    "ai.perplexity.comet",               # Perplexity Comet
    "com.perplexity.comet",
    "org.mozilla.firefox",
    "org.mozilla.nightly",
    "com.brave.Browser",
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
    "com.anthropic.claudefordesktop",    # Claude Desktop
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

    # Browsers always allow paste — web apps use custom roles we can't classify
    if bundle_id and bundle_id in _BROWSER_BUNDLE_IDS:
        log.debug("Known browser bundle %s — always paste", bundle_id)
        return True, bundle_id

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
            # No focused element reported — still try to paste, Cmd+V is generally safe
            log.debug("No focused element reported — fail open")
            return True, bundle_id

        # Read its role
        err, role = AX.AXUIElementCopyAttributeValue(
            focused, AX.kAXRoleAttribute, None
        )
        if err != 0 or role is None:
            return True, bundle_id  # fail open

        role_str = str(role)
        is_text = role_str in _TEXT_ROLES

        if not is_text:
            # Check if element has an editable value
            try:
                err, value = AX.AXUIElementCopyAttributeValue(
                    focused, AX.kAXValueAttribute, None
                )
                if err == 0 and value is not None and isinstance(value, str):
                    is_text = True
            except Exception:
                pass

        if not is_text:
            # Check subrole — some fields report via subrole only
            try:
                err, subrole = AX.AXUIElementCopyAttributeValue(
                    focused, AX.kAXSubroleAttribute, None
                )
                if err == 0 and subrole is not None:
                    subrole_str = str(subrole)
                    # Common text-like subroles
                    if subrole_str in ("AXSecureTextField", "AXContentList"):
                        is_text = True
            except Exception:
                pass

        log.debug("AX focus: app=%s role=%s → text=%s", bundle_id, role_str, is_text)
        return is_text, bundle_id

    except Exception as e:
        log.debug("AX focus check failed: %s — fail open", e)
        return True, bundle_id
