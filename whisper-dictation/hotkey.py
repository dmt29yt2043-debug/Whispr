"""Right Option key detection via pynput in a subprocess.

pynput's CGEventTap needs its own CFRunLoop, which conflicts with
rumps/NSApplication on the main thread. Solution: run the listener
in a separate process and communicate via a pipe.

Supports two modes:
  1. Hold mode: hold Right Option to record, release to stop
  2. Toggle mode: double-tap Right Option to start, tap again to stop
"""

import time
import logging
import multiprocessing
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Timing
_DOUBLE_TAP_WINDOW = 0.35  # seconds
_MIN_HOLD = 0.15  # seconds


def _listener_process(conn):
    """Run in a subprocess — listens for Right Option and sends events via pipe."""
    # Silence all logging in the child to avoid duplicate messages in parent's log
    import logging as _logging
    _logging.getLogger().handlers.clear()
    _logging.getLogger().addHandler(_logging.NullHandler())

    # Also suppress stdout/stderr in the child (parent captures only via pipe events)
    import sys, os as _os
    try:
        devnull = open(_os.devnull, "w")
        sys.stdout = devnull
        sys.stderr = devnull
    except Exception:
        pass

    from pynput import keyboard

    TRIGGER = keyboard.Key.alt_r

    def on_press(key):
        if key == TRIGGER:
            try:
                conn.send(("down", time.time()))
            except (BrokenPipeError, OSError):
                return False  # stop listener

    def on_release(key):
        if key == TRIGGER:
            try:
                conn.send(("up", time.time()))
            except (BrokenPipeError, OSError):
                return False  # stop listener

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    listener.join()


class FnKeyHandler:
    """Detects Right Option key via pynput in a subprocess."""

    def __init__(self, on_start: Callable, on_stop: Callable):
        self._on_start = on_start
        self._on_stop = on_stop

        self._key_down = False
        self._key_down_time = 0.0
        self._last_tap_time = 0.0
        self._toggle_mode = False
        self._recording = False
        # Keep references to prevent garbage collection
        self._proc = None
        self._parent_conn = None
        self._child_conn = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """Start the key listener subprocess and a reader thread."""
        self._parent_conn, self._child_conn = multiprocessing.Pipe(duplex=False)

        self._proc = multiprocessing.Process(
            target=_listener_process, args=(self._child_conn,), daemon=True
        )
        self._proc.start()

        reader = threading.Thread(target=self._read_events, args=(self._parent_conn,), daemon=True)
        reader.start()

        log.info("Right Option key listener started (subprocess)")

    def _read_events(self, conn) -> None:
        """Read key events from the subprocess pipe."""
        while True:
            try:
                event_type, timestamp = conn.recv()
                if event_type == "down":
                    self._handle_down(timestamp)
                elif event_type == "up":
                    self._handle_up(timestamp)
            except (EOFError, OSError):
                log.warning("Key listener subprocess ended")
                break

    def _handle_down(self, now: float) -> None:
        if self._key_down:
            return  # ignore repeats
        self._key_down = True
        self._key_down_time = now

        # Toggle mode: currently recording -> stop
        if self._toggle_mode and self._recording:
            self._toggle_mode = False
            self._recording = False
            log.info("Key press -> stop (toggle mode)")
            self._on_stop()
            return

        # Start recording
        self._recording = True
        self._toggle_mode = False
        log.info("Key press -> start recording")
        self._on_start()

    def _handle_up(self, now: float) -> None:
        if not self._key_down:
            return
        self._key_down = False
        hold_duration = now - self._key_down_time

        # Toggle mode: ignore release
        if self._toggle_mode:
            return

        if not self._recording:
            self._last_tap_time = now
            return

        if hold_duration < _MIN_HOLD:
            time_since_last_tap = now - self._last_tap_time
            if time_since_last_tap < _DOUBLE_TAP_WINDOW:
                self._toggle_mode = True
                log.info("Double-tap -> toggle mode ON")
                self._last_tap_time = 0.0
            else:
                self._recording = False
                log.info("Short tap -> cancel")
                self._on_stop()
                self._last_tap_time = now
        else:
            self._recording = False
            log.info("Key release -> stop (hold %.2fs)", hold_duration)
            self._on_stop()
            self._last_tap_time = now
