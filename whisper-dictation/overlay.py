"""Floating overlay window — shows recording/processing status.

A small pill-shaped translucent window at the top center of the screen,
similar to Whispr Flow's progress indicator.
"""

import logging
import threading
from typing import Optional

from AppKit import (
    NSApplication,
    NSWindow,
    NSView,
    NSColor,
    NSFont,
    NSTextField,
    NSProgressIndicator,
    NSScreen,
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSMakeRect,
)
import AppKit
from PyObjCTools import AppHelper

log = logging.getLogger(__name__)

_WINDOW_WIDTH = 280
_WINDOW_HEIGHT = 44
_CORNER_RADIUS = 14


class StatusOverlay:
    """Floating pill-shaped overlay showing recording/processing status."""

    def __init__(self):
        self._window = None  # type: Optional[NSWindow]
        self._label = None
        self._progress = None
        self._pulse_timer = None

    def _ensure_window(self):
        """Create the overlay window lazily on first use."""
        if self._window is not None:
            return

        # Position: top center of main screen
        screen = NSScreen.mainScreen()
        if not screen:
            return
        sf = screen.frame()
        x = (sf.size.width - _WINDOW_WIDTH) / 2
        y = sf.size.height - _WINDOW_HEIGHT - 80  # below notch/menu bar

        rect = NSMakeRect(x, y, _WINDOW_WIDTH, _WINDOW_HEIGHT)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            0,  # NSWindowStyleMaskBorderless
            NSBackingStoreBuffered,
            False,
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setOpaque_(False)
        self._window.setAlphaValue_(0.92)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setIgnoresMouseEvents_(True)
        self._window.setAnimationBehavior_(2)  # NSWindowAnimationBehaviorUtilityWindow
        self._window.setCollectionBehavior_(1 | 16)  # CanJoinAllSpaces | Stationary

        # Content view with rounded corners
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT))
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(_CORNER_RADIUS)
        content.layer().setMasksToBounds_(True)
        content.layer().setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.15, 0.15, 0.15, 0.95).CGColor()
        )

        # Label
        self._label = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 12, _WINDOW_WIDTH - 32, 20))
        self._label.setEditable_(False)
        self._label.setBordered_(False)
        self._label.setDrawsBackground_(False)
        self._label.setTextColor_(NSColor.whiteColor())
        self._label.setFont_(NSFont.systemFontOfSize_weight_(13, 0.3))
        self._label.setStringValue_("Ready")
        content.addSubview_(self._label)

        # Progress bar
        self._progress = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(16, 4, _WINDOW_WIDTH - 32, 4)
        )
        self._progress.setStyle_(0)  # NSProgressIndicatorStyleBar
        self._progress.setIndeterminate_(True)
        self._progress.setHidden_(True)
        content.addSubview_(self._progress)

        self._window.setContentView_(content)

    def show_recording(self):
        """Show 'Recording...' with pulsing indicator."""
        def _do():
            self._ensure_window()
            if not self._window:
                return
            self._label.setStringValue_("  Recording...")
            self._label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(1.0, 0.35, 0.35, 1.0))
            self._progress.setHidden_(True)
            self._window.orderFront_(None)
        AppHelper.callAfter(_do)

    def show_processing(self):
        """Show 'Processing...' with progress bar."""
        def _do():
            self._ensure_window()
            if not self._window:
                return
            self._label.setStringValue_("  Processing...")
            self._label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.4, 0.7, 1.0, 1.0))
            self._progress.setHidden_(False)
            self._progress.startAnimation_(None)
            self._window.orderFront_(None)
        AppHelper.callAfter(_do)

    def show_done(self, text=""):
        """Briefly show 'Done' then hide."""
        def _do():
            self._ensure_window()
            if not self._window:
                return
            preview = text[:40] + "..." if len(text) > 40 else text
            self._label.setStringValue_("  " + preview)
            self._label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.4, 1.0, 0.5, 1.0))
            self._progress.setHidden_(True)
            self._progress.stopAnimation_(None)
            self._window.orderFront_(None)
        AppHelper.callAfter(_do)

        # Hide after 2 seconds
        threading.Timer(2.0, self.hide).start()

    def show_error(self, message="Error"):
        """Show error briefly then hide."""
        def _do():
            self._ensure_window()
            if not self._window:
                return
            self._label.setStringValue_("  " + message)
            self._label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(1.0, 0.4, 0.3, 1.0))
            self._progress.setHidden_(True)
            self._progress.stopAnimation_(None)
            self._window.orderFront_(None)
        AppHelper.callAfter(_do)

        threading.Timer(3.0, self.hide).start()

    def hide(self):
        """Hide the overlay."""
        def _do():
            if self._window:
                self._window.orderOut_(None)
        AppHelper.callAfter(_do)
