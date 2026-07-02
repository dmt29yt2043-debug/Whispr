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


def _stroke_mic_glyph(size: float, cx: float, cy: float, line_w: float) -> None:
    """Stroke the mic glyph into the currently-focused graphics context.

    Caller sets the stroke colour beforehand. Geometry is intentionally
    identical to overlay._OverlayView._draw_mic so every rendition of the
    mic (overlay, menu bar, app icon) has the same character.
    """
    from AppKit import NSBezierPath, NSMakeRect

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


def _draw_mic_into_context(size_pt: int, color_tuple=(0.0, 0.0, 0.0, 1.0)) -> "NSImage":  # type: ignore
    """Build an NSImage (size_pt × size_pt) with the mic glyph on a
    transparent background. Used for menu-bar icons."""
    from AppKit import NSImage, NSColor, NSMakeSize

    img = NSImage.alloc().initWithSize_(NSMakeSize(size_pt, size_pt))
    img.lockFocus()
    try:
        r, g, b, a = color_tuple
        NSColor.colorWithRed_green_blue_alpha_(r, g, b, a).set()
        size = float(size_pt)
        line_w = max(1.0, size / 14.0)  # slightly bolder for small renders
        _stroke_mic_glyph(size, size / 2.0, size / 2.0, line_w)
    finally:
        img.unlockFocus()
    return img


def _draw_app_icon(size_pt: int) -> "NSImage":  # type: ignore
    """App icon: navy mic on a LIGHT rounded-rect card.

    The old app icon was the bare glyph on transparency — macOS Tahoe
    composites transparent app icons onto a dark grey squircle, which
    made the icon look dim next to other apps. We now paint our own
    light card (soft white→pale-blue vertical gradient) so the tile
    reads bright in Launchpad/Finder/Dock.
    """
    from AppKit import (
        NSImage, NSColor, NSGradient, NSBezierPath, NSMakeRect, NSMakeSize,
    )

    img = NSImage.alloc().initWithSize_(NSMakeSize(size_pt, size_pt))
    img.lockFocus()
    try:
        size = float(size_pt)
        # Apple's icon grid: artwork occupies ~82% of the canvas.
        inset = size * 0.09
        card = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
        radius = (size - 2 * inset) * 0.225  # squircle-ish corner
        card_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            card, radius, radius
        )

        # Light card: white at the top → pale periwinkle at the bottom.
        top = NSColor.colorWithRed_green_blue_alpha_(0.99, 0.99, 1.00, 1.0)
        bottom = NSColor.colorWithRed_green_blue_alpha_(0.88, 0.91, 0.98, 1.0)
        NSGradient.alloc().initWithStartingColor_endingColor_(top, bottom) \
            .drawInBezierPath_angle_(card_path, -90.0)

        # Hairline border so the card doesn't melt into white backgrounds.
        NSColor.colorWithRed_green_blue_alpha_(0.13, 0.19, 0.48, 0.18).set()
        card_path.setLineWidth_(max(1.0, size / 128.0))
        card_path.stroke()

        # Navy mic, same hue as overlay/menu bar.
        NSColor.colorWithRed_green_blue_alpha_(0.13, 0.19, 0.48, 1.0).set()
        glyph_size = size * 0.56
        line_w = max(1.0, glyph_size / 16.0)
        _stroke_mic_glyph(glyph_size, size / 2.0, size / 2.0, line_w)
    finally:
        img.unlockFocus()
    return img


def build_app_icns(dest_icns: str) -> bool:
    """Render the light app icon at all Apple iconset sizes → .icns.

    Writes a temporary .iconset directory next to dest_icns and compiles
    it with iconutil. Returns True on success.
    """
    import shutil
    import subprocess

    iconset_dir = os.path.splitext(dest_icns)[0] + ".iconset"
    os.makedirs(iconset_dir, exist_ok=True)
    sizes = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    try:
        for px, name in sizes:
            img = _draw_app_icon(px)
            if not _save_png(img, os.path.join(iconset_dir, name)):
                return False
        subprocess.run(
            ["iconutil", "-c", "icns", "-o", dest_icns, iconset_dir],
            check=True, capture_output=True,
        )
        return True
    except Exception as e:
        log.warning("build_app_icns failed: %s", e)
        return False
    finally:
        shutil.rmtree(iconset_dir, ignore_errors=True)


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
