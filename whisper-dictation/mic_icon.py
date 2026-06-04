"""Render the app's signature mic glyph as PNG files for use as icons.

We already draw a recognizable line-art microphone inside the overlay
(see _OverlayView._draw_mic in overlay.py). This module recreates the
exact same shape in a stand-alone NSImage so we can use it everywhere:

  - Menu bar (rumps icon) — as a "template image". macOS auto-tints
    template images to match the menu bar appearance (light/dark mode,
    selected state, etc.), so we draw the mic in pure black on a clear
    background and let the system handle colour.
  - LaunchPad / Finder / Dock — generated icon.icns (separate path,
    larger and colour-rendered).

Files are written under ~/.whisper-dictation/icons/ on first launch and
re-rendered if missing. They're tiny (< 5 KB each) and regenerating is
cheap.

Note: This file uses AppKit/Foundation directly. It must NOT be imported
from worker subprocesses — only from the main app process.
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_ICON_DIR = os.path.expanduser("~/.whisper-dictation/icons")
_MENU_BAR_PT = 22  # standard macOS menu bar height in points


def _draw_mic_into_context(size_pt: int, color_tuple=(0.0, 0.0, 0.0, 1.0)) -> "NSImage":  # type: ignore
    """Build an NSImage of size (size_pt × size_pt) containing the mic glyph.

    Geometry is intentionally identical to overlay._OverlayView._draw_mic
    so the icon looks like a smaller version of what the user sees while
    recording — same character, same shape.
    """
    from AppKit import (
        NSImage, NSBezierPath, NSColor, NSMakeRect, NSMakeSize,
    )

    img = NSImage.alloc().initWithSize_(NSMakeSize(size_pt, size_pt))
    img.lockFocus()
    try:
        r, g, b, a = color_tuple
        NSColor.colorWithRed_green_blue_alpha_(r, g, b, a).set()

        size = float(size_pt)
        cx = size / 2.0
        cy = size / 2.0
        line_w = max(1.0, size / 14.0)  # slightly bolder than overlay for small render

        # === Head capsule ===
        head_w = size * 0.42
        head_h = size * 0.52
        head_x = cx - head_w / 2
        head_y = cy - head_h / 2 + size * 0.08
        head_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(head_x, head_y, head_w, head_h), head_w / 2, head_w / 2
        )
        head_path.setLineWidth_(line_w)
        head_path.stroke()

        # === 3 grille lines ===
        n = 3
        inset_x = head_w * 0.22
        ly0 = head_y + head_h * 0.22
        ly1 = head_y + head_h * 0.78
        for i in range(n):
            t = i / (n - 1)
            ly = ly0 + t * (ly1 - ly0)
            g_path = NSBezierPath.bezierPath()
            g_path.moveToPoint_((head_x + inset_x, ly))
            g_path.lineToPoint_((head_x + head_w - inset_x, ly))
            g_path.setLineWidth_(line_w * 0.8)
            g_path.setLineCapStyle_(1)
            g_path.stroke()

        # === U-arc stand ===
        arc_w = size * 0.58
        arc_x = cx - arc_w / 2
        arc_top_y = head_y - size * 0.04
        arc_bot_y = arc_top_y - size * 0.14
        arc = NSBezierPath.bezierPath()
        arc.moveToPoint_((arc_x, arc_top_y))
        arc.curveToPoint_controlPoint1_controlPoint2_(
            (cx + arc_w / 2, arc_top_y),
            (arc_x, arc_bot_y - size * 0.02),
            (cx + arc_w / 2, arc_bot_y - size * 0.02),
        )
        arc.setLineWidth_(line_w)
        arc.setLineCapStyle_(1)
        arc.stroke()

        # === Stem ===
        stem_top = arc_bot_y + size * 0.01
        stem_bot = stem_top - size * 0.14
        stem = NSBezierPath.bezierPath()
        stem.moveToPoint_((cx, stem_top))
        stem.lineToPoint_((cx, stem_bot))
        stem.setLineWidth_(line_w)
        stem.setLineCapStyle_(1)
        stem.stroke()

        # === Base ===
        base_w = size * 0.30
        base = NSBezierPath.bezierPath()
        base.moveToPoint_((cx - base_w / 2, stem_bot))
        base.lineToPoint_((cx + base_w / 2, stem_bot))
        base.setLineWidth_(line_w)
        base.setLineCapStyle_(1)
        base.stroke()
    finally:
        img.unlockFocus()
    return img


def _save_png(img: "NSImage", path: str) -> bool:  # type: ignore
    """Serialize an NSImage to a PNG file. Returns True on success."""
    from AppKit import NSBitmapImageRep, NSPNGFileType

    try:
        tiff = img.TIFFRepresentation()
        if tiff is None:
            return False
        rep = NSBitmapImageRep.imageRepWithData_(tiff)
        png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
        with open(path, "wb") as f:
            f.write(bytes(png_data))
        return True
    except Exception as e:
        log.warning("Failed to write PNG to %s: %s", path, e)
        return False


def ensure_menu_bar_icon() -> Optional[str]:
    """Generate (if missing) and return path to the menu-bar PNG.

    Coloured (dark navy) rather than template-black: on macOS Tahoe (26),
    template PNGs with anti-aliased edges sometimes don't render in the
    menu bar at all. A coloured icon shows up reliably in both light and
    dark mode. The colour we use is the same dark navy as the overlay,
    so the menu-bar and overlay glyphs look like the same product.
    """
    os.makedirs(_ICON_DIR, exist_ok=True)
    path = os.path.join(_ICON_DIR, "menu_bar_mic.png")

    # Always regenerate — it's cheap (< 5 ms) and ensures the file matches
    # the current overlay geometry if either file changes.
    try:
        # Same dark navy as overlay's _draw_mic: NSColor(0.13, 0.19, 0.48, 1.0)
        img = _draw_mic_into_context(_MENU_BAR_PT, color_tuple=(0.13, 0.19, 0.48, 1.0))
        if _save_png(img, path):
            log.info("Menu-bar icon written: %s", path)
            return path
    except Exception as e:
        log.warning("ensure_menu_bar_icon failed: %s", e)
    return None


def ensure_menu_bar_icon_recording() -> Optional[str]:
    """Variant of the menu-bar icon used while actively recording.

    Solid red (not template) — we want the recording state to stand out
    in the menu bar regardless of dark/light mode. Pure red disables the
    template auto-tinting, so callers should set template=False when
    swapping to this icon.
    """
    os.makedirs(_ICON_DIR, exist_ok=True)
    path = os.path.join(_ICON_DIR, "menu_bar_mic_rec.png")
    try:
        # Deep red — visible on both light and dark menu bars
        img = _draw_mic_into_context(
            _MENU_BAR_PT, color_tuple=(0.85, 0.15, 0.15, 1.0)
        )
        if _save_png(img, path):
            return path
    except Exception as e:
        log.warning("ensure_menu_bar_icon_recording failed: %s", e)
    return None
