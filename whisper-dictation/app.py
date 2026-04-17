#!/usr/bin/env python3
"""Whisper Dictation — macOS menu bar voice dictation app.

Hold Right Option to record, release to transcribe and paste.
Double-tap Right Option for hands-free toggle mode.
"""

import os
import sys
import logging
import threading

import rumps
from dotenv import load_dotenv

# Override Python's app identity BEFORE rumps/AppKit starts using it.
# This changes the icon in alerts/dialogs from Python to our custom one.
def _set_app_identity():
    try:
        from AppKit import NSBundle, NSImage, NSApplication
        # Set bundle name + display name (shown in alerts)
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Whisper Dictation"
            info["CFBundleDisplayName"] = "Whisper Dictation"

        # Set app icon
        here = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(here, "icon.icns")
        if os.path.exists(icon_path):
            app = NSApplication.sharedApplication()
            img = NSImage.alloc().initWithContentsOfFile_(icon_path)
            if img is not None:
                app.setApplicationIconImage_(img)
    except Exception:
        pass

_set_app_identity()

from recorder import Recorder
from transcriber import transcribe, warmup_local_model
from cleaner import clean_text
from injector import inject_text
from replacements import apply_replacements, load_replacements, save_replacements
from stats import record_words, get_words_today, get_words_week, get_words_month
from sounds import play_start, play_stop
from hotkey import FnKeyHandler
from fn_hotkey import FnHotkey
from overlay import StatusOverlay
from focus_check import get_focused_text_info
import settings as S
import vad

# Load .env: try user config dir first, then project dir
_config_dir = os.path.expanduser("~/.whisper-dictation")
load_dotenv(os.path.join(_config_dir, ".env"))

if not getattr(sys, 'frozen', False):
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"))
    load_dotenv(os.path.join(os.path.dirname(_here), ".env"))

# Set up logging ONLY in the main process.
# multiprocessing child workers re-import this module as "__mp_main__";
# adding handlers there would duplicate every log line.
_IS_MAIN = __name__ == "__main__" or __name__ == "app"

_log_file = os.path.expanduser("~/.whisper-dictation/app.log")
os.makedirs(os.path.dirname(_log_file), exist_ok=True)

_root = logging.getLogger()
_root.setLevel(logging.INFO)

if _IS_MAIN:
    for _h in list(_root.handlers):
        _root.removeHandler(_h)
    _fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    _fh = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _root.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _root.addHandler(_sh)
else:
    # Child process — silence everything
    _root.handlers.clear()
    _root.addHandler(logging.NullHandler())

log = logging.getLogger("app")

ICON_IDLE = "🎙"
ICON_REC = "● REC"
ICON_PROCESSING = "⏳"


class WhisperDictationApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_IDLE, quit_button=None)

        # Ensure settings exist on disk
        S.load()

        self.recorder = Recorder(force_builtin=S.get("force_builtin_mic", True))

        # Hotkey: if user selected "fn", try CGEventTap on main run loop.
        # Otherwise (or if Fn fails to receive events) use pynput subprocess.
        self._use_fn = S.get("hotkey", "right_option") == "fn"
        self.fn_hotkey = None
        if self._use_fn:
            self.fn_hotkey = FnHotkey(on_start=self._on_record_start, on_stop=self._on_record_stop)
        self.hotkey = FnKeyHandler(on_start=self._on_record_start, on_stop=self._on_record_stop)

        self.overlay = StatusOverlay()

        # Menu items
        self.record_item = rumps.MenuItem("🔴 Start Recording", callback=self._toggle_recording)
        self.stats_item = rumps.MenuItem("📊 Statistics", callback=self._show_stats)
        self.replacements_item = rumps.MenuItem("🔄 Text Replacements", callback=self._show_replacements)
        self.settings_item = rumps.MenuItem("⚙️ Settings", callback=self._show_settings)
        self.quit_item = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self.record_item,
            rumps.separator,
            self.stats_item,
            self.replacements_item,
            self.settings_item,
            rumps.separator,
            self.quit_item,
        ]

        # Warmup local model in background if needed
        if S.get("mode") in (S.MODE_LOCAL, S.MODE_AUTO):
            threading.Thread(target=warmup_local_model, daemon=True).start()

    # ── Recording lifecycle ─────────────────────────────────────────────

    def _toggle_recording(self, sender) -> None:
        if self.recorder.is_recording:
            self._on_record_stop()
        else:
            self._on_record_start()

    def _on_record_start(self) -> None:
        self.recorder.start()
        play_start()
        self.title = ICON_REC
        self.record_item.title = "⏹ Stop Recording"
        self.overlay.show_recording()
        # Capture bundle ID at recording start (before we lose focus)
        try:
            _, self._recording_bundle_id = get_focused_text_info()
        except Exception:
            self._recording_bundle_id = None
        log.info("Recording started (app: %s)", self._recording_bundle_id)

    def _on_record_stop(self) -> None:
        audio_path = self.recorder.stop()
        play_stop()

        if audio_path is None:
            self.title = ICON_IDLE
            self.record_item.title = "🔴 Start Recording"
            self.overlay.hide()
            log.info("Recording too short, ignored")
            return

        self.title = ICON_PROCESSING
        self.overlay.show_processing()
        log.info("Processing audio: %s", audio_path)

        threading.Thread(target=self._process_audio, args=(audio_path,), daemon=True).start()

    def _process_audio(self, audio_path: str) -> None:
        """Full pipeline: VAD → transcribe → replace → clean → inject."""
        temp_files = [audio_path]
        try:
            # Step 1: VAD — strip silence
            if S.get("vad_enabled", True):
                vad_path = vad.strip_silence(audio_path)
                if vad_path is None:
                    log.info("VAD: no speech detected")
                    self.overlay.show_error("No speech detected")
                    self._reset_ui()
                    return
                if vad_path != audio_path:
                    temp_files.append(vad_path)
                    audio_path = vad_path

            # Step 2: Transcribe (with anti-hallucination)
            raw_text = transcribe(audio_path)
            if not raw_text:
                self.overlay.show_error("No speech detected")
                self._reset_ui()
                return

            # Step 3: Apply text replacements (exact-match overrides cleanup)
            replaced_text = apply_replacements(raw_text)
            if replaced_text != raw_text:
                final_text = replaced_text
                log.info("Replacement applied")
            else:
                # Step 4: GPT cleanup with per-app tone
                final_text = clean_text(raw_text, bundle_id=self._recording_bundle_id)

            # Step 5: Record stats
            record_words(final_text)

            # Step 6: Inject with focus check + clipboard restore
            result = inject_text(
                final_text,
                check_focus=S.get("check_focus", True),
                restore_clipboard=S.get("restore_clipboard", True),
            )

            if result == "copied":
                self.overlay.show_done("📋 " + final_text)
            else:
                self.overlay.show_done(final_text)

            self._reset_ui()
            log.info("Done (%s): '%s'", result, final_text[:80])

        except Exception as e:
            log.error("Processing failed: %s", e, exc_info=True)
            self.overlay.show_error(str(e)[:40])
            self._reset_ui()
        finally:
            for p in temp_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _reset_ui(self) -> None:
        self.title = ICON_IDLE
        self.record_item.title = "🔴 Start Recording"

    # ── Menu handlers ───────────────────────────────────────────────────

    def _notify(self, title: str, message: str) -> None:
        rumps.notification(title="Whisper Dictation", subtitle=title, message=message)

    def _show_stats(self, _sender=None) -> None:
        rumps.alert(
            title="📊 Dictation Statistics",
            message=(
                f"Today:       {get_words_today():,} words\n"
                f"Last 7 days: {get_words_week():,} words\n"
                f"Last 30 days: {get_words_month():,} words"
            ),
        )

    def _show_settings(self, _sender=None) -> None:
        """Show settings dialog — mode, tone, hotkey, toggles."""
        current_mode = S.get("mode", S.MODE_AUTO)
        cleanup_on = S.get("cleanup_enabled", True)
        tone = S.get("base_tone", S.TONE_NEUTRAL)
        force_mic = S.get("force_builtin_mic", True)
        vad_on = S.get("vad_enabled", True)
        check_focus = S.get("check_focus", True)
        restore_clip = S.get("restore_clipboard", False)
        always_en = S.get("always_english", False)
        hotkey = S.get("hotkey", "right_option")

        def _check(v): return "✓" if v else " "

        summary = (
            f"Current settings:\n\n"
            f"Hotkey:  {hotkey}\n"
            f"Mode:    {current_mode.upper()}\n"
            f"Tone:    {tone}\n\n"
            f"[{_check(cleanup_on)}] GPT cleanup (filler removal)\n"
            f"[{_check(force_mic)}] Force built-in microphone\n"
            f"[{_check(vad_on)}] Voice Activity Detection\n"
            f"[{_check(check_focus)}] Check text focus before paste\n"
            f"[{_check(restore_clip)}] Restore clipboard after paste\n"
            f"[{_check(always_en)}] Always translate to English\n"
        )

        response = rumps.alert(
            title="⚙️ Settings",
            message=summary,
            ok="Change Hotkey",
            cancel="Close",
            other="More...",
        )

        if response == 1:
            self._change_hotkey()
        elif response == 2:
            self._change_other_setting()

    def _change_hotkey(self) -> None:
        current = S.get("hotkey", "right_option")
        message = (
            f"Current hotkey: {current}\n\n"
            "Available keys:\n"
            "  fn             (⌐ Fn/Globe — experimental, may be suppressed by macOS)\n"
            "  right_option   (⌥ right, recommended)\n"
            "  left_option    (⌥ left)\n"
            "  right_cmd      (⌘ right)\n"
            "  right_shift    (⇧ right)\n"
            "  right_ctrl     (⌃ right)\n"
            "  caps_lock      (⇪)\n"
            "  f13...f19      (function row on big keyboards)\n\n"
            "App restart required after changing to/from 'fn'.\n"
            "Type the key name below:"
        )
        w = rumps.Window(
            message=message, title="Change Hotkey",
            default_text=current, ok="Save", cancel="Cancel",
            dimensions=(300, 25),
        )
        r = w.run()
        if not r.clicked:
            return
        new_key = r.text.strip().lower()
        valid = {"fn", "right_option", "left_option", "right_cmd", "left_cmd",
                 "right_shift", "left_shift", "right_ctrl", "caps_lock",
                 "f13", "f14", "f15", "f16", "f17", "f18", "f19"}
        if new_key in valid:
            prev = S.get("hotkey")
            S.set("hotkey", new_key)
            # Fn requires restart (CGEventTap on main run loop)
            if new_key == "fn" or prev == "fn":
                rumps.alert("Restart Required",
                            message=f"Hotkey set to '{new_key}'. Please quit and relaunch the app.")
            else:
                self.hotkey.restart_with_new_key()
                self._notify("Hotkey", f"Changed to: {new_key}")
            log.info("Hotkey changed to: %s", new_key)
        else:
            rumps.alert("Invalid", message=f"Unknown key: {new_key}")

    def _change_mode(self) -> None:
        current = S.get("mode", S.MODE_AUTO)
        message = (
            f"Current mode: {current.upper()}\n\n"
            "Enter new mode:\n"
            "  cloud  — OpenAI API only (needs internet)\n"
            "  local  — faster-whisper offline (no cleanup)\n"
            "  auto   — cloud first, local fallback\n"
        )
        w = rumps.Window(
            message=message,
            title="Change Mode",
            default_text=current,
            ok="Save",
            cancel="Cancel",
            dimensions=(200, 25),
        )
        r = w.run()
        if not r.clicked:
            return
        new_mode = r.text.strip().lower()
        if new_mode in (S.MODE_CLOUD, S.MODE_LOCAL, S.MODE_AUTO):
            S.set("mode", new_mode)
            self._notify("Settings", f"Mode set to: {new_mode}")
            log.info("Mode changed to: %s", new_mode)
            if new_mode in (S.MODE_LOCAL, S.MODE_AUTO):
                threading.Thread(target=warmup_local_model, daemon=True).start()
        else:
            rumps.alert("Invalid Mode", message="Use: cloud, local, or auto")

    def _change_other_setting(self) -> None:
        message = (
            "Type a setting name and new value, space-separated:\n\n"
            "mode <cloud|local|auto>\n"
            "tone <neutral|professional|casual|raw>\n"
            "cleanup <on|off>\n"
            "mic <on|off>           (force built-in mic)\n"
            "vad <on|off>           (voice activity detection)\n"
            "focus <on|off>         (check text focus)\n"
            "restore <on|off>       (restore clipboard)\n"
            "english <on|off>       (always translate to English)\n"
            "style <text>           (user style hint)\n\n"
            "Example: tone professional"
        )
        w = rumps.Window(
            message=message,
            title="Change Setting",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 25),
        )
        r = w.run()
        if not r.clicked or not r.text.strip():
            return

        parts = r.text.strip().split(None, 1)
        if len(parts) < 2:
            rumps.alert("Invalid", message="Enter: <key> <value>")
            return
        key, value = parts[0].lower(), parts[1].strip()
        bool_map = {"on": True, "off": False, "true": True, "false": False, "yes": True, "no": False}

        if key == "mode":
            if value in (S.MODE_CLOUD, S.MODE_LOCAL, S.MODE_AUTO):
                S.set("mode", value)
                self._notify("Settings", f"Mode: {value}")
                if value in (S.MODE_LOCAL, S.MODE_AUTO):
                    threading.Thread(target=warmup_local_model, daemon=True).start()
            else:
                rumps.alert("Invalid", message="Use: cloud, local, or auto")
        elif key == "tone":
            if value in (S.TONE_NEUTRAL, S.TONE_PROFESSIONAL, S.TONE_CASUAL, S.TONE_RAW):
                S.set("base_tone", value)
                self._notify("Settings", f"Tone: {value}")
            else:
                rumps.alert("Invalid", message="Use: neutral, professional, casual, or raw")
        elif key == "cleanup":
            if value in bool_map:
                S.set("cleanup_enabled", bool_map[value])
                self._notify("Settings", f"Cleanup: {value}")
        elif key == "mic":
            if value in bool_map:
                S.set("force_builtin_mic", bool_map[value])
                self.recorder = Recorder(force_builtin=bool_map[value])
                self._notify("Settings", f"Built-in mic: {value}")
        elif key == "vad":
            if value in bool_map:
                S.set("vad_enabled", bool_map[value])
                self._notify("Settings", f"VAD: {value}")
        elif key == "focus":
            if value in bool_map:
                S.set("check_focus", bool_map[value])
                self._notify("Settings", f"Focus check: {value}")
        elif key == "restore":
            if value in bool_map:
                S.set("restore_clipboard", bool_map[value])
                self._notify("Settings", f"Clipboard restore: {value}")
        elif key == "english":
            if value in bool_map:
                S.set("always_english", bool_map[value])
                self._notify("Settings", f"Always English: {value}")
        elif key == "style":
            S.set("user_style", value)
            self._notify("Settings", f"Style saved")
        else:
            rumps.alert("Unknown setting", message=f"Unknown key: {key}")

    def _show_replacements(self, _sender=None) -> None:
        replacements = load_replacements()
        current = "\n".join(f'• "{k}" → "{v}"' for k, v in replacements.items()) or "(no replacements configured)"
        response = rumps.alert(
            title="🔄 Text Replacements",
            message=f"Current replacements:\n{current}\n\nClick 'Add New' to add.",
            ok="Add New",
            cancel="Close",
            other="Remove",
        )
        if response == 1:
            self._add_replacement(replacements)
        elif response == 2:
            self._remove_replacement(replacements)

    def _add_replacement(self, replacements: dict) -> None:
        w1 = rumps.Window(
            message="Trigger phrase (what you say):",
            title="Add Replacement", default_text="", ok="Next", cancel="Cancel", dimensions=(300, 25),
        )
        r1 = w1.run()
        if not r1.clicked or not r1.text.strip():
            return
        trigger = r1.text.strip()
        w2 = rumps.Window(
            message=f'Replacement text for "{trigger}":',
            title="Add Replacement", default_text="", ok="Save", cancel="Cancel", dimensions=(300, 25),
        )
        r2 = w2.run()
        if not r2.clicked or not r2.text.strip():
            return
        replacements[trigger.lower()] = r2.text.strip()
        save_replacements(replacements)
        self._notify("Replacement Added", f'"{trigger}" → "{r2.text.strip()}"')

    def _remove_replacement(self, replacements: dict) -> None:
        if not replacements:
            rumps.alert("Nothing to Remove", message="No replacements configured.")
            return
        w = rumps.Window(
            message="Enter the trigger phrase to remove:",
            title="Remove Replacement", default_text="", ok="Remove", cancel="Cancel", dimensions=(300, 25),
        )
        r = w.run()
        if not r.clicked or not r.text.strip():
            return
        key = r.text.strip().lower()
        if key in replacements:
            del replacements[key]
            save_replacements(replacements)
            self._notify("Replacement Removed", f'Removed "{key}"')
        else:
            rumps.alert("Not Found", message=f'"{key}" is not in the replacements list.')

    def _quit(self, _sender=None) -> None:
        rumps.quit_application()


def main():
    app = WhisperDictationApp()

    # Try Fn (CGEventTap on main run loop) if selected
    fn_installed = False
    if app.fn_hotkey is not None:
        fn_installed = app.fn_hotkey.install()
        if fn_installed:
            log.info("Fn hotkey tap installed — will fall back to pynput if no events after 30s")
        else:
            log.warning("Fn hotkey tap install failed — falling back to pynput")

    # Always start pynput subprocess as fallback/primary
    app.hotkey.start()

    # If Fn was requested, monitor whether it actually receives events
    if app.fn_hotkey is not None and fn_installed:
        def _fn_watchdog():
            import time as _t
            _t.sleep(30)
            if not app.fn_hotkey.seen_fn_event:
                log.warning(
                    "Fn tap received 0 events after 30s — Fn is suppressed by macOS. "
                    "Use a different hotkey (right_option, caps_lock, etc.)."
                )
        threading.Thread(target=_fn_watchdog, daemon=True).start()

    log.info("Whisper Dictation started (mode=%s, hotkey=%s).",
             S.get("mode"), S.get("hotkey"))
    app.run()


if __name__ == "__main__":
    main()
