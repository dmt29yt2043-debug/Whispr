"""Configurable hotkey detection via pynput in a subprocess.

pynput's CGEventTap needs its own CFRunLoop, which conflicts with
rumps/NSApplication on the main thread. Solution: run the listener
in a separate process and communicate via a pipe.

Supported keys: right_option, left_option, right_cmd, left_cmd,
                right_shift, left_shift, caps_lock, f13..f19, right_ctrl

Modes:
  1. Hold mode: hold key to record, release to stop
  2. Toggle mode: double-tap to start, tap again to stop
"""

import time
import logging
import multiprocessing
import threading
from typing import Callable, Optional

import settings as S

log = logging.getLogger(__name__)

# Timing
_DOUBLE_TAP_WINDOW = 0.35  # seconds
_MIN_HOLD = 0.08  # seconds — shorter than this = probably accidental tap


def _resolve_pynput_key(key_name: str):
    """Map a setting name → pynput Key object."""
    from pynput import keyboard
    mapping = {
        "right_option": keyboard.Key.alt_r,
        "left_option":  keyboard.Key.alt,
        "right_cmd":    keyboard.Key.cmd_r,
        "left_cmd":     keyboard.Key.cmd,
        "right_shift":  keyboard.Key.shift_r,
        "left_shift":   keyboard.Key.shift,
        "right_ctrl":   keyboard.Key.ctrl_r,
        "caps_lock":    keyboard.Key.caps_lock,
        "f13":          keyboard.Key.f13,
        "f14":          keyboard.Key.f14,
        "f15":          keyboard.Key.f15,
        "f16":          keyboard.Key.f16,
        "f17":          keyboard.Key.f17,
        "f18":          keyboard.Key.f18,
        "f19":          keyboard.Key.f19,
    }
    return mapping.get(key_name, keyboard.Key.alt_r)


def _listener_process(conn, key_name: str):
    """Run in a subprocess — listens for the chosen key."""
    # Silence logging/stdout in child
    import logging as _logging
    _logging.getLogger().handlers.clear()
    _logging.getLogger().addHandler(_logging.NullHandler())

    import sys, os as _os
    try:
        devnull = open(_os.devnull, "w")
        sys.stdout = devnull
        sys.stderr = devnull
    except Exception:
        pass

    from pynput import keyboard

    TRIGGER = _resolve_pynput_key(key_name)

    def on_press(key):
        if key == TRIGGER:
            try:
                conn.send(("down", time.time()))
            except (BrokenPipeError, OSError):
                return False

    def on_release(key):
        if key == TRIGGER:
            try:
                conn.send(("up", time.time()))
            except (BrokenPipeError, OSError):
                return False

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    listener.join()


class FnKeyHandler:
    """Detects the configured hotkey via pynput in a subprocess."""

    def __init__(self, on_start: Callable, on_stop: Callable):
        self._on_start = on_start
        self._on_stop = on_stop

        self._key_down = False
        self._key_down_time = 0.0
        self._last_tap_time = 0.0
        self._toggle_mode = False
        self._recording = False
        self._proc = None
        self._parent_conn = None
        self._child_conn = None
        self._current_key = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def current_key_name(self) -> str:
        return self._current_key or S.get("hotkey", "right_option")

    def start(self) -> None:
        """Start the key listener subprocess and a reader thread."""
        key_name = S.get("hotkey", "right_option")
        self._current_key = key_name
        self._parent_conn, self._child_conn = multiprocessing.Pipe(duplex=False)

        self._proc = multiprocessing.Process(
            target=_listener_process, args=(self._child_conn, key_name), daemon=True
        )
        self._proc.start()

        reader = threading.Thread(target=self._read_events, args=(self._parent_conn,), daemon=True)
        reader.start()

        log.info("Hotkey listener started: %s (subprocess)", key_name)

    def restart_with_new_key(self) -> None:
        """Stop current listener and start with new key from settings."""
        try:
            if self._proc is not None:
                self._proc.terminate()
                self._proc.join(timeout=1)
        except Exception:
            pass
        self._proc = None
        self._parent_conn = None
        self._child_conn = None
        self.start()

    def _read_events(self, conn) -> None:
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
            return
        self._key_down = True
        self._key_down_time = now

        if self._toggle_mode and self._recording:
            self._toggle_mode = False
            self._recording = False
            log.info("Key press -> stop (toggle mode)")
            self._on_stop()
            return

        self._recording = True
        self._toggle_mode = False
        log.info("Key press -> start recording")
        self._on_start()

    def _handle_up(self, now: float) -> None:
        if not self._key_down:
            return
        self._key_down = False
        hold_duration = now - self._key_down_time

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
