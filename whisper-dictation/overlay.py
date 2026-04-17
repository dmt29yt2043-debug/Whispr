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

# ── Sizing ───────────────────────────────────────────────────────────
_WINDOW_W = 180
_WINDOW_H = 44
_CORNER_RADIUS = _WINDOW_H / 2  # full pill
_MARGIN_RIGHT = 20
_MARGIN_TOP = 6

# ── Equalizer ────────────────────────────────────────────────────────
_BAR_COUNT = 14
_BAR_WIDTH = 3
_BAR_GAP = 4
_BAR_MIN_HEIGHT = 4
_BAR_MAX_HEIGHT = 22

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
        """Draw a simple blue-gradient microphone icon (vector, no image)."""
        # Mic body capsule
        body_w = size * 0.50
        body_h = size * 0.65
        body_x = center_x - body_w / 2
        body_y = center_y - body_h / 2 + size * 0.05

        # Gradient fill for body
        mic_top = NSColor.colorWithRed_green_blue_alpha_(0.35, 0.55, 0.95, 1.0)
        mic_bot = NSColor.colorWithRed_green_blue_alpha_(0.20, 0.30, 0.75, 1.0)
        body_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(body_x, body_y, body_w, body_h), body_w / 2, body_w / 2
        )
        gradient = NSGradient.alloc().initWithStartingColor_endingColor_(mic_bot, mic_top)
        gradient.drawInBezierPath_angle_(body_path, 90.0)

        # Base / stand U-shape under mic
        stand_color = NSColor.colorWithRed_green_blue_alpha_(0.20, 0.30, 0.75, 1.0)
        stand_color.set()
        arc_w = size * 0.70
        arc_h = size * 0.25
        arc_x = center_x - arc_w / 2
        arc_y = body_y - arc_h * 0.3
        arc_path = NSBezierPath.bezierPath()
        arc_path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            (center_x, arc_y), arc_w / 2, 180.0, 360.0
        )
        arc_path.setLineWidth_(max(1.6, size / 14))
        arc_path.stroke()

        # Stand vertical bar
        stand_w = max(1.6, size / 14)
        stand_x = center_x - stand_w / 2
        stand_y_top = arc_y - stand_w / 2
        stand_y_bot = arc_y - size * 0.18
        stand_rect = NSMakeRect(stand_x, stand_y_bot, stand_w, stand_y_top - stand_y_bot)
        NSBezierPath.bezierPathWithRect_(stand_rect).fill()

        # Stand base (horizontal)
        base_w = size * 0.32
        base_h = stand_w
        base_x = center_x - base_w / 2
        base_y = stand_y_bot
        NSBezierPath.bezierPathWithRect_(
            NSMakeRect(base_x, base_y, base_w, base_h)
        ).fill()

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
                # Voice levels; blend current level with neighbor positions
                # and add a little per-bar variation for visual life
                base = self._levels[i]
                # Vary with a subtle sine for pleasant look even during quiet moments
                variation = 0.08 * math.sin(self._phase * 3 + i * 0.7)
                lvl = max(0.05, min(1.0, base + variation))
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
        self._level_history.append(max(0.0, min(1.0, float(level))))
        if len(self._level_history) > _BAR_COUNT:
            self._level_history = self._level_history[-_BAR_COUNT:]

        def _do():
            if self._view:
                self._view.setLevels_(self._level_history)
        AppHelper.callAfter(_do)

    def show_recording(self):
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
        def _do():
            self._ensure_window()
            if not self._window or not self._view:
                return
            self._state = STATE_PROCESSING
            self._view.setMode_(STATE_PROCESSING)
            self._window.orderFront_(None)
            self._start_animation_timer()
        AppHelper.callAfter(_do)

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

        def _delayed_hide():
            import time as _t
            _t.sleep(1.0)
            self.hide()
        self._hide_thread = threading.Thread(target=_delayed_hide, daemon=True)
        self._hide_thread.start()

    def show_error(self, _message: str = "Error"):
        def _do():
            self._ensure_window()
            if not self._window or not self._view:
                return
            self._state = STATE_DONE
            self._view.setMode_(STATE_DONE)
            self._window.orderFront_(None)
        AppHelper.callAfter(_do)

        def _delayed_hide():
            import time as _t
            _t.sleep(1.5)
            self.hide()
        threading.Thread(target=_delayed_hide, daemon=True).start()

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
