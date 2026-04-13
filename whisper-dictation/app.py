#!/usr/bin/env python3
"""Whisper Dictation — macOS menu bar voice dictation app.

Hold Fn to record, release to transcribe and paste.
Double-tap Fn for hands-free toggle mode.
"""

import os
import sys
import logging
import threading

import rumps
from dotenv import load_dotenv

from recorder import Recorder
from transcriber import transcribe
from cleaner import clean_text
from injector import inject_text
from replacements import apply_replacements, load_replacements, save_replacements
from stats import record_words, get_words_today, get_words_week, get_words_month
from sounds import play_start, play_stop
from hotkey import FnKeyHandler
from overlay import StatusOverlay

# Load .env: try user config dir first, then project dir
_config_dir = os.path.expanduser("~/.whisper-dictation")
load_dotenv(os.path.join(_config_dir, ".env"))

if not getattr(sys, 'frozen', False):
    # Running from source — also check project dir
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"))
    load_dotenv(os.path.join(os.path.dirname(_here), ".env"))

_log_file = os.path.expanduser("~/.whisper-dictation/app.log")
os.makedirs(os.path.dirname(_log_file), exist_ok=True)

# Force configure root logger (imported modules may have already called basicConfig)
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
_fh = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
_fh.setFormatter(_fmt)
_root.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_sh)

log = logging.getLogger("app")

ICON_IDLE = "🎙"
ICON_REC = "● REC"
ICON_PROCESSING = "⏳"


class WhisperDictationApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_IDLE, quit_button=None)

        self.recorder = Recorder()
        self.hotkey = FnKeyHandler(on_start=self._on_record_start, on_stop=self._on_record_stop)
        self.overlay = StatusOverlay()

        # Menu items
        self.record_item = rumps.MenuItem("🔴 Start Recording", callback=self._toggle_recording)
        self.stats_item = rumps.MenuItem("📊 Statistics", callback=self._show_stats)
        self.replacements_item = rumps.MenuItem("🔄 Text Replacements", callback=self._show_replacements)
        self.separator = rumps.separator
        self.quit_item = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self.record_item,
            self.stats_item,
            self.replacements_item,
            self.separator,
            self.quit_item,
        ]

    def _toggle_recording(self, sender) -> None:
        """Toggle recording on/off via menu click."""
        if self.recorder.is_recording:
            self._on_record_stop()
        else:
            self._on_record_start()

    def _on_record_start(self) -> None:
        """Called by hotkey handler when recording should start."""
        self.recorder.start()
        play_start()
        self.title = ICON_REC
        self.record_item.title = "⏹ Stop Recording"
        self.overlay.show_recording()
        log.info("Recording started")

    def _on_record_stop(self) -> None:
        """Called by hotkey handler when recording should stop."""
        audio_path = self.recorder.stop()
        play_stop()

        if audio_path is None:
            # Too short or no audio
            self.title = ICON_IDLE
            self.record_item.title = "🔴 Start Recording"
            self.overlay.hide()
            log.info("Recording too short, ignored")
            return

        self.title = ICON_PROCESSING
        self.overlay.show_processing()
        log.info("Processing audio: %s", audio_path)

        # Process in background thread to not block the event tap
        threading.Thread(target=self._process_audio, args=(audio_path,), daemon=True).start()

    def _process_audio(self, audio_path: str) -> None:
        """Transcribe, clean, replace, inject — runs in a background thread."""
        try:
            # Step 1: Transcribe
            raw_text = transcribe(audio_path)
            if not raw_text:
                self.overlay.show_error("No speech detected")
                self.title = ICON_IDLE
                self.record_item.title = "🔴 Start Recording"
                return

            # Step 2: Apply text replacements (before cleanup)
            replaced_text = apply_replacements(raw_text)

            # Step 3: Clean up (skip if replacement was applied — it's already exact)
            if replaced_text != raw_text:
                final_text = replaced_text
            else:
                final_text = clean_text(raw_text)

            # Step 4: Record stats
            record_words(final_text)

            # Step 5: Inject into focused app
            inject_text(final_text)

            self.title = ICON_IDLE
            self.record_item.title = "🔴 Start Recording"
            self.overlay.show_done(final_text)
            log.info("Done: '%s'", final_text[:80])

        except Exception as e:
            log.error("Processing failed: %s", e)
            self.overlay.show_error(str(e)[:40])
            self.title = ICON_IDLE
            self.record_item.title = "🔴 Start Recording"
        finally:
            # Clean up temp file
            try:
                os.unlink(audio_path)
            except OSError:
                pass

    def _notify(self, title: str, message: str) -> None:
        """Show a macOS notification."""
        rumps.notification(
            title="Whisper Dictation",
            subtitle=title,
            message=message,
        )

    def _show_stats(self, _sender=None) -> None:
        """Show statistics in a dialog."""
        today = get_words_today()
        week = get_words_week()
        month = get_words_month()

        rumps.alert(
            title="📊 Dictation Statistics",
            message=(
                f"Today:       {today:,} words\n"
                f"Last 7 days: {week:,} words\n"
                f"Last 30 days: {month:,} words"
            ),
        )

    def _show_replacements(self, _sender=None) -> None:
        """Show text replacements management dialog."""
        replacements = load_replacements()

        if replacements:
            lines = [f"• \"{k}\" → \"{v}\"" for k, v in replacements.items()]
            current = "\n".join(lines)
        else:
            current = "(no replacements configured)"

        response = rumps.alert(
            title="🔄 Text Replacements",
            message=(
                f"Current replacements:\n{current}\n\n"
                "To add/edit, click 'Add New'.\n"
                "To remove, click 'Remove'."
            ),
            ok="Add New",
            cancel="Close",
            other="Remove",
        )

        if response == 1:  # Add New
            self._add_replacement(replacements)
        elif response == 2:  # Remove
            self._remove_replacement(replacements)

    def _add_replacement(self, replacements: dict) -> None:
        """Dialog to add a new text replacement."""
        window = rumps.Window(
            message="Trigger phrase (what you say):",
            title="Add Replacement",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(300, 25),
        )
        trigger_response = window.run()
        if not trigger_response.clicked or not trigger_response.text.strip():
            return

        trigger = trigger_response.text.strip()

        window2 = rumps.Window(
            message=f"Replacement text for \"{trigger}\":",
            title="Add Replacement",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(300, 25),
        )
        value_response = window2.run()
        if not value_response.clicked or not value_response.text.strip():
            return

        replacements[trigger.lower()] = value_response.text.strip()
        save_replacements(replacements)
        self._notify("Replacement Added", f"\"{trigger}\" → \"{value_response.text.strip()}\"")

    def _remove_replacement(self, replacements: dict) -> None:
        """Dialog to remove a text replacement."""
        if not replacements:
            rumps.alert("Nothing to Remove", message="No replacements configured.")
            return

        window = rumps.Window(
            message="Enter the trigger phrase to remove:",
            title="Remove Replacement",
            default_text="",
            ok="Remove",
            cancel="Cancel",
            dimensions=(300, 25),
        )
        response = window.run()
        if not response.clicked or not response.text.strip():
            return

        key = response.text.strip().lower()
        if key in replacements:
            del replacements[key]
            save_replacements(replacements)
            self._notify("Replacement Removed", f"Removed \"{key}\"")
        else:
            rumps.alert("Not Found", message=f"\"{key}\" is not in the replacements list.")

    def _quit(self, _sender=None) -> None:
        rumps.quit_application()


def main():
    app = WhisperDictationApp()

    # Start hotkey listener
    app.hotkey.start()

    log.info("Whisper Dictation started. Hold Right Option to dictate.")
    app.run()


if __name__ == "__main__":
    main()
