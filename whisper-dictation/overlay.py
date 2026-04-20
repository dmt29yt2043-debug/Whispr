"""Floating overlay pill near the top-right with:
- Light gradient background (light blue → light purple)
- White rounded frame
- During RECORDING: microphone icon + animated equalizer bars (synced to voice)
- During PROCESSING: no mic, just animated equalizer (sine wave)
- Bars use a horizontal cyan→purple color gradient matching the app icon.
"""

import logging
import threading
import math
import os
from typing import Optional, List

import objc
from AppKit import (
    NSWindow, NSView, NSColor, NSBezierPath, NSImage, NSScreen,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSMakeRect, NSGradient, NSShadow, NSMakeSize,
    NSCompositingOperationSourceOver, NSCompositingOperationSourceAtop,
)
from Foundation import NSTimer
from PyObjCTools import AppHelper

log = logging.getLogger(__name__)

# ── Sizing (40% smaller than previous 180x44) ────────────────────────
_WINDOW_W = 108
_WINDOW_H = 26
_CORNER_RADIUS = _WINDOW_H / 2  # full pill
_MARGIN_RIGHT = 18
_MARGIN_TOP = 6

# ── Equalizer ────────────────────────────────────────────────────────
_BAR_COUNT = 14
_BAR_WIDTH = 2
_BAR_GAP = 2
_BAR_MIN_HEIGHT = 3
_BAR_MAX_HEIGHT = 14

# Colors for the gradient (cyan → purple)
_BAR_COLOR_START = (0.22, 0.62, 0.96)   # cyan-blue
_BAR_COLOR_END = (0.56, 0.30, 0.85)     # purple

# Background gradient
_BG_COLOR_TOP = (0.85, 0.93, 1.00, 0.96)
_BG_COLOR_BOTTOM = (0.90, 0.86, 1.00, 0.96)

# Mic area width (only drawn during RECORDING)
_MIC_AREA_W = 30

STATE_HIDDEN = "hidden"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_DONE = "done"


def _lerp(a, b, t):
    return a + (b - a) * t


def _color_at(t: float):
    """Interpolated color along the cyan→purple gradient, t in [0,1]."""
    t = max(0.0, min(1.0, t))
    r = _lerp(_BAR_COLOR_START[0], _BAR_COLOR_END[0], t)
    g = _lerp(_BAR_COLOR_START[1], _BAR_COLOR_END[1], t)
    b = _lerp(_BAR_COLOR_START[2], _BAR_COLOR_END[2], t)
    return r, g, b


class _OverlayView(NSView):
    """Custom view that draws the pill + bars + optional mic."""

    def initWithFrame_(self, frame):
        self = objc.super(_OverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._levels: List[float] = [0.0] * _BAR_COUNT
        self._mode = STATE_HIDDEN
        self._phase = 0.0
        return self

    def _draw_mic(self, center_x: float, center_y: float, size: float):
        """Draw the line-art microphone from the reference image.

        Dark navy blue outline style:
          - Capsule-shaped head with 3 horizontal grille lines
          - U-shaped stand arc under the head
          - Thin vertical stem
          - Short horizontal base
        """
        # Dark navy blue matching the reference
        mic_color = NSColor.colorWithRed_green_blue_alpha_(0.13, 0.19, 0.48, 1.0)
        mic_color.set()

        line_w = max(1.0, size / 18.0)

        # Head capsule (outlined, not filled)
        head_w = size * 0.42
        head_h = size * 0.52
        head_x = center_x - head_w / 2
        head_y = center_y - head_h / 2 + size * 0.08
        head_rect = NSMakeRect(head_x, head_y, head_w, head_h)
        head_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            head_rect, head_w / 2, head_w / 2
        )
        head_path.setLineWidth_(line_w)
        head_path.stroke()

        # 3 horizontal grille lines inside the head
        n_lines = 3
        inset_x = head_w * 0.22
        line_y_start = head_y + head_h * 0.22
        line_y_end = head_y + head_h * 0.78
        for i in range(n_lines):
            t = i / (n_lines - 1) if n_lines > 1 else 0.5
            ly = line_y_start + t * (line_y_end - line_y_start)
            grille = NSBezierPath.bezierPath()
            grille.moveToPoint_((head_x + inset_x, ly))
            grille.lineToPoint_((head_x + head_w - inset_x, ly))
            grille.setLineWidth_(line_w * 0.8)
            grille.setLineCapStyle_(1)  # round cap
            grille.stroke()

        # U-shaped stand arc under the head
        arc_w = size * 0.58
        arc_x = center_x - arc_w / 2
        arc_top_y = head_y - size * 0.04   # top of arc (where it meets head sides)
        arc_bot_y = arc_top_y - size * 0.14  # bottom of arc

        arc = NSBezierPath.bezierPath()
        # Draw a half-ellipse opening upward (U-shape)
        # Using cubic curves for smoothness
        arc.moveToPoint_((arc_x, arc_top_y))
        arc.curveToPoint_controlPoint1_controlPoint2_(
            (center_x + arc_w / 2, arc_top_y),
            (arc_x, arc_bot_y - size * 0.02),
            (center_x + arc_w / 2, arc_bot_y - size * 0.02),
        )
        arc.setLineWidth_(line_w)
        arc.setLineCapStyle_(1)
        arc.stroke()

        # Vertical stem below the arc
        stem_top = arc_bot_y + size * 0.01
        stem_bot = stem_top - size * 0.14
        stem = NSBezierPath.bezierPath()
        stem.moveToPoint_((center_x, stem_top))
        stem.lineToPoint_((center_x, stem_bot))
        stem.setLineWidth_(line_w)
        stem.setLineCapStyle_(1)
        stem.stroke()

        # Horizontal base
        base_w = size * 0.30
        base_y = stem_bot
        base = NSBezierPath.bezierPath()
        base.moveToPoint_((center_x - base_w / 2, base_y))
        base.lineToPoint_((center_x + base_w / 2, base_y))
        base.setLineWidth_(line_w)
        base.setLineCapStyle_(1)
        base.stroke()

    def setLevels_(self, levels):
        self._levels = list(levels[-_BAR_COUNT:])
        while len(self._levels) < _BAR_COUNT:
            self._levels.insert(0, 0.0)
        self.setNeedsDisplay_(True)

    def setMode_(self, mode):
        self._mode = mode
        self.setNeedsDisplay_(True)

    def advancePhase(self):
        self._phase += 0.22
        if self._phase > 1000:
            self._phase = 0.0
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self._mode == STATE_HIDDEN:
            return

        bounds = self.bounds()
        bw = bounds.size.width
        bh = bounds.size.height

        # 1. Light gradient background pill
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(1, 1, bw - 2, bh - 2), _CORNER_RADIUS, _CORNER_RADIUS
        )
        bg_top = NSColor.colorWithRed_green_blue_alpha_(*_BG_COLOR_TOP)
        bg_bot = NSColor.colorWithRed_green_blue_alpha_(*_BG_COLOR_BOTTOM)
        gradient = NSGradient.alloc().initWithStartingColor_endingColor_(bg_bot, bg_top)
        gradient.drawInBezierPath_angle_(pill, 90.0)

        # 2. White frame (stroke)
        NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.95).set()
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(1, 1, bw - 2, bh - 2), _CORNER_RADIUS, _CORNER_RADIUS
        )
        frame.setLineWidth_(1.8)
        frame.stroke()

        # 3. Content area (bars, optionally mic on left)
        content_left_pad = 12
        content_right_pad = 12

        if self._mode == STATE_RECORDING:
            # Draw vector microphone on the left
            mic_size = bh - 12
            mic_center_x = content_left_pad + mic_size / 2
            mic_center_y = bh / 2
            self._draw_mic(mic_center_x, mic_center_y, mic_size)
            bars_x_start = content_left_pad + mic_size + 6
        else:
            bars_x_start = content_left_pad

        bars_x_end = bw - content_right_pad
        bars_area_w = bars_x_end - bars_x_start
        total_bars_w = _BAR_COUNT * _BAR_WIDTH + (_BAR_COUNT - 1) * _BAR_GAP

        # Center bars horizontally within the bars area
        offset_x = bars_x_start + (bars_area_w - total_bars_w) / 2

        # 4. Draw each bar with its color and height
        for i in range(_BAR_COUNT):
            t = i / (_BAR_COUNT - 1)  # position 0..1 across the bars

            # Determine height based on mode
            if self._mode == STATE_RECORDING:
                # Base level from history (each bar shows a recent moment)
                base = self._levels[i]
                # Add strong per-bar modulation so bars "dance" around the voice
                # Mix two sine waves at different frequencies + phase per bar
                sine1 = math.sin(self._phase * 4 + i * 0.9)
                sine2 = math.sin(self._phase * 2.3 + i * 0.42 + 1.1)
                # Variation amplitude scales with the base level — loud = more dance
                amp = 0.18 + base * 0.45
                variation = amp * (0.6 * sine1 + 0.4 * sine2)
                lvl = base + variation
                # Exaggerate with a non-linear curve so bars hit the top more often
                lvl = max(0.08, min(1.0, lvl * 1.25))
            elif self._mode == STATE_PROCESSING:
                # Two overlapping sine waves for organic motion
                a = math.sin(self._phase * 2 + i * 0.55)
                b = math.sin(self._phase * 1.2 + i * 0.35)
                lvl = 0.5 + 0.35 * a + 0.15 * b
                lvl = max(0.1, min(1.0, (lvl + 1) / 2))
            elif self._mode == STATE_DONE:
                lvl = 0.4
            else:
                lvl = 0.1

            h = _BAR_MIN_HEIGHT + (_BAR_MAX_HEIGHT - _BAR_MIN_HEIGHT) * lvl
            x = offset_x + i * (_BAR_WIDTH + _BAR_GAP)
            y = (bh - h) / 2

            r, g, b = _color_at(t)
            NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0).set()
            bar_rect = NSMakeRect(x, y, _BAR_WIDTH, h)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar_rect, _BAR_WIDTH / 2, _BAR_WIDTH / 2
            ).fill()


class StatusOverlay:
    """Pill window with equalizer near the top-right of the main screen."""

    def __init__(self):
        self._window: Optional[NSWindow] = None
        self._view: Optional[_OverlayView] = None
        self._timer = None
        self._level_history: List[float] = [0.0] * _BAR_COUNT
        self._state = STATE_HIDDEN
        self._hide_thread = None
        # Epoch counter: every call to show_recording/show_processing
        # increments it. Pending _delayed_hide threads check the epoch
        # before hiding — if a newer state has started, they skip.
        self._epoch = 0

    def _ensure_window(self):
        if self._window is not None:
            return

        screen = NSScreen.mainScreen()
        if not screen:
            return
        sf = screen.frame()
        x = sf.size.width - _WINDOW_W - _MARGIN_RIGHT
        y = sf.size.height - _WINDOW_H - 32

        rect = NSMakeRect(x, y, _WINDOW_W, _WINDOW_H)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, 0, NSBackingStoreBuffered, False,
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setOpaque_(False)
        self._window.setAlphaValue_(1.0)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setIgnoresMouseEvents_(True)
        self._window.setCollectionBehavior_(1 | 16)

        # Soft shadow under the pill
        self._window.setHasShadow_(True)

        view = _OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, _WINDOW_W, _WINDOW_H))
        self._window.setContentView_(view)
        self._view = view

    def push_level(self, level: float) -> None:
        if not self._view:
            return
        # BUG FIX #18: build a fresh list in one go so the closure
        # doesn't see a mid-update state.
        clamped = max(0.0, min(1.0, float(level)))
        new_history = (self._level_history + [clamped])[-_BAR_COUNT:]
        self._level_history = new_history

        snapshot = list(new_history)
        def _do():
            if self._view:
                self._view.setLevels_(snapshot)
        AppHelper.callAfter(_do)

    def show_recording(self):
        self._epoch += 1
        def _do():
            self._ensure_window()
            if not self._window or not self._view:
                return
            self._state = STATE_RECORDING
            self._level_history = [0.1] * _BAR_COUNT
            self._view.setLevels_(self._level_history)
            self._view.setMode_(STATE_RECORDING)
            self._window.orderFront_(None)
            self._start_animation_timer()
        AppHelper.callAfter(_do)

    def show_processing(self):
        self._epoch += 1
        def _do():
            self._ensure_window()
            if not self._window or not self._view:
                return
            self._state = STATE_PROCESSING
            self._view.setMode_(STATE_PROCESSING)
            self._window.orderFront_(None)
            self._start_animation_timer()
        AppHelper.callAfter(_do)

    def _schedule_hide(self, delay: float) -> None:
        """BUG FIX #17: schedule a hide tied to the current epoch. If
        a newer show_* is called before the delay elapses, that show
        bumps the epoch and the pending hide no-ops."""
        self._epoch += 1
        my_epoch = self._epoch

        def _delayed_hide():
            import time as _t
            _t.sleep(delay)
            if self._epoch == my_epoch:
                self.hide()
        threading.Thread(target=_delayed_hide, daemon=True).start()

    def show_done(self, _text: str = ""):
        def _do():
            self._ensure_window()
            if not self._window or not self._view:
                return
            self._state = STATE_DONE
            self._view.setMode_(STATE_DONE)
            self._window.orderFront_(None)
            self._stop_animation_timer()
        AppHelper.callAfter(_do)
        self._schedule_hide(1.0)

    def show_error(self, _message: str = "Error"):
        def _do():
            self._ensure_window()
            if not self._window or not self._view:
                return
            self._state = STATE_DONE
            self._view.setMode_(STATE_DONE)
            self._window.orderFront_(None)
        AppHelper.callAfter(_do)
        self._schedule_hide(1.5)

    def hide(self):
        def _do():
            self._stop_animation_timer()
            if self._window:
                self._window.orderOut_(None)
                self._state = STATE_HIDDEN
        AppHelper.callAfter(_do)

    def _start_animation_timer(self):
        self._stop_animation_timer()

        def _tick_():
            if self._view and self._state in (STATE_RECORDING, STATE_PROCESSING):
                self._view.advancePhase()
            elif self._view:
                self._view.setNeedsDisplay_(True)

        def _schedule():
            self._timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.045, True, lambda t: _tick_()
            )
        AppHelper.callAfter(_schedule)

    def _stop_animation_timer(self):
        if self._timer:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None
