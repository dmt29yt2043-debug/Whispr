"""Floating overlay — small pill near the menu bar (top-right) with animated
equalizer bars during recording, spinner during processing, and quick
checkmark on done.

Inspired by Whispr Flow / OpenWhispr's pill window.
"""

import logging
import threading
import math
from typing import Optional, List

import objc
from AppKit import (
    NSWindow, NSView, NSColor, NSBezierPath, NSFont, NSTextField,
    NSScreen, NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSMakeRect, NSMakePoint, NSMakeSize,
    NSRectFillUsingOperation, NSCompositingOperationSourceOver,
)
from Foundation import NSTimer, NSObject, NSRunLoop, NSDefaultRunLoopMode
from PyObjCTools import AppHelper

log = logging.getLogger(__name__)

# ── Sizing ───────────────────────────────────────────────────────────
_WINDOW_W = 130
_WINDOW_H = 28
_CORNER_RADIUS = 14
_MARGIN_RIGHT = 20        # distance from right edge of screen
_MARGIN_TOP = 6           # distance from bottom of menu bar

# ── Equalizer ────────────────────────────────────────────────────────
_BAR_COUNT = 6
_BAR_WIDTH = 3
_BAR_GAP = 3
_BAR_MIN_HEIGHT = 3
_BAR_MAX_HEIGHT = 16

# ── States ───────────────────────────────────────────────────────────
STATE_HIDDEN = "hidden"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_DONE = "done"


class _EqualizerView(NSView):
    """Custom view that draws animated bars based on an audio level history."""

    def initWithFrame_(self, frame):
        self = objc.super(_EqualizerView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._levels: List[float] = [0.0] * _BAR_COUNT
        self._mode = STATE_HIDDEN  # 'recording', 'processing', 'done'
        self._phase = 0.0  # animation phase for idle/processing
        return self

    def setLevels_(self, levels):
        """Set the level history; length should equal _BAR_COUNT, values 0..1."""
        self._levels = list(levels[-_BAR_COUNT:])
        while len(self._levels) < _BAR_COUNT:
            self._levels.insert(0, 0.0)
        self.setNeedsDisplay_(True)

    def setMode_(self, mode):
        self._mode = mode
        self.setNeedsDisplay_(True)

    def advancePhase(self):
        self._phase += 0.18
        if self._phase > 100:
            self._phase = 0.0
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        # Background
        NSColor.colorWithRed_green_blue_alpha_(0.10, 0.10, 0.12, 0.95).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), _CORNER_RADIUS, _CORNER_RADIUS
        ).fill()

        if self._mode == STATE_HIDDEN:
            return

        bw = self.bounds().size.width
        bh = self.bounds().size.height

        # Draw icon dot on the left (red for recording, blue for processing, green for done)
        dot_r = 3.0
        dot_x = 10
        dot_y = bh / 2
        if self._mode == STATE_RECORDING:
            NSColor.colorWithRed_green_blue_alpha_(1.0, 0.30, 0.30, 1.0).set()
        elif self._mode == STATE_PROCESSING:
            NSColor.colorWithRed_green_blue_alpha_(0.40, 0.70, 1.0, 1.0).set()
        elif self._mode == STATE_DONE:
            NSColor.colorWithRed_green_blue_alpha_(0.35, 0.95, 0.50, 1.0).set()
        else:
            NSColor.whiteColor().set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(dot_x - dot_r, dot_y - dot_r, dot_r * 2, dot_r * 2)
        ).fill()

        # Bars area (right of the dot)
        bars_x_start = dot_x + dot_r + 8
        total_bars_w = _BAR_COUNT * _BAR_WIDTH + (_BAR_COUNT - 1) * _BAR_GAP
        bars_x_start = bw - total_bars_w - 12  # right-align bars

        if self._mode == STATE_RECORDING:
            # Color: soft mint/green
            NSColor.colorWithRed_green_blue_alpha_(0.35, 0.95, 0.50, 0.95).set()
        elif self._mode == STATE_PROCESSING:
            NSColor.colorWithRed_green_blue_alpha_(0.50, 0.80, 1.0, 0.95).set()
        elif self._mode == STATE_DONE:
            NSColor.colorWithRed_green_blue_alpha_(0.35, 0.95, 0.50, 0.90).set()
        else:
            NSColor.whiteColor().set()

        for i in range(_BAR_COUNT):
            if self._mode == STATE_RECORDING:
                # Use levels history (newest on right)
                lvl = self._levels[i]
            elif self._mode == STATE_PROCESSING:
                # Sine wave animation
                lvl = 0.5 + 0.5 * math.sin(self._phase + i * 0.7)
            elif self._mode == STATE_DONE:
                # Static mid-height
                lvl = 0.45
            else:
                lvl = 0.1

            h = _BAR_MIN_HEIGHT + (_BAR_MAX_HEIGHT - _BAR_MIN_HEIGHT) * max(0.0, min(1.0, lvl))
            x = bars_x_start + i * (_BAR_WIDTH + _BAR_GAP)
            y = (bh - h) / 2
            rect = NSMakeRect(x, y, _BAR_WIDTH, h)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                rect, _BAR_WIDTH / 2, _BAR_WIDTH / 2
            ).fill()


class StatusOverlay:
    """Pill window with equalizer animation near the top-right of main screen."""

    def __init__(self):
        self._window: Optional[NSWindow] = None
        self._view: Optional[_EqualizerView] = None
        self._timer = None
        self._level_history: List[float] = [0.0] * _BAR_COUNT
        self._state = STATE_HIDDEN
        self._hide_timer_thread = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def _ensure_window(self):
        if self._window is not None:
            return

        screen = NSScreen.mainScreen()
        if not screen:
            return
        sf = screen.frame()
        # Top-right position, below menu bar
        x = sf.size.width - _WINDOW_W - _MARGIN_RIGHT
        # menu bar is ~25 pts; position the pill just below it
        y = sf.size.height - _WINDOW_H - 32

        rect = NSMakeRect(x, y, _WINDOW_W, _WINDOW_H)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, 0, NSBackingStoreBuffered, False,
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setOpaque_(False)
        self._window.setAlphaValue_(0.96)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setIgnoresMouseEvents_(True)
        self._window.setCollectionBehavior_(1 | 16)  # all spaces + stationary

        view = _EqualizerView.alloc().initWithFrame_(NSMakeRect(0, 0, _WINDOW_W, _WINDOW_H))
        self._window.setContentView_(view)
        self._view = view

    # ── Public API ───────────────────────────────────────────────────

    def push_level(self, level: float) -> None:
        """Recorder pushes a new audio level here every chunk."""
        if not self._view:
            return
        # Shift history left, append new value (0..1)
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
            self._level_history = [0.0] * _BAR_COUNT
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

        # Auto-hide after 1.2s
        def _delayed_hide():
            import time as _t
            _t.sleep(1.2)
            self.hide()
        self._hide_timer_thread = threading.Thread(target=_delayed_hide, daemon=True)
        self._hide_timer_thread.start()

    def show_error(self, _message: str = "Error"):
        # Reuse done state with red dot would be nice; for minimal overlay
        # we just briefly flash then hide.
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

    # ── Animation timer (for processing sine wave + redraw) ──────────

    def _start_animation_timer(self):
        self._stop_animation_timer()

        def _tick_():
            if self._view:
                if self._state == STATE_PROCESSING:
                    self._view.advancePhase()
                else:
                    # Just force redraw to keep recent levels rendered
                    self._view.setNeedsDisplay_(True)

        def _schedule():
            self._timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.055, True, lambda t: _tick_()
            )
        AppHelper.callAfter(_schedule)

    def _stop_animation_timer(self):
        if self._timer:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None
