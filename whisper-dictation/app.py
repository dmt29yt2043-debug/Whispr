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

# Only the MAIN process should touch NSApplication (set name/icon/policy).
# multiprocessing children re-import this module as __mp_main__ — if we set
# the activation policy there too, the child shows up in the Dock.
_IS_MAIN_PROC = __name__ in ("__main__", "app")


def _set_app_identity():
    """Set bundle name + icon. Activation policy is handled separately."""
    try:
        from AppKit import NSBundle, NSImage, NSApplication
        app = NSApplication.sharedApplication()

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Whisper Dictation"
            info["CFBundleDisplayName"] = "Whisper Dictation"

        here = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(here, "icon.icns")
        if os.path.exists(icon_path):
            img = NSImage.alloc().initWithContentsOfFile_(icon_path)
            if img is not None:
                app.setApplicationIconImage_(img)
    except Exception:
        pass


def _hide_from_dock_later():
    """Switch to accessory mode AFTER rumps has created the status item.

    NSStatusItem must be added while the app is in Regular mode; after that
    we can flip to accessory to remove the Dock icon.
    """
    import time as _t
    from AppKit import NSApplication
    from PyObjCTools import AppHelper
    _t.sleep(1.2)  # let rumps register its status item
    AppHelper.callAfter(
        lambda: NSApplication.sharedApplication().setActivationPolicy_(1)
    )


def _hide_child_from_dock():
    """For multiprocessing children — hide from Dock but don't load the icon."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except Exception:
        pass


if _IS_MAIN_PROC:
    _set_app_identity()
else:
    _hide_child_from_dock()

from recorder import Recorder
from transcriber import transcribe, warmup_local_model
from cleaner import clean_text
from injector import inject_text
from replacements import apply_replacements, load_replacements, save_replacements
from stats import (
    record_words, get_words_today, get_words_week, get_words_month,
    get_usage_today, get_usage_week, get_usage_month, get_usage_all,
)
from sounds import play_start, play_stop
from hotkey import FnKeyHandler
from repaste_hotkey import RePasteHotkey
from overlay import StatusOverlay
from streaming_transcriber import StreamingTranscriber
import injector as _injector
from focus_check import get_focused_text_info
import settings as S
import vad

# Load .env from every plausible location. BUG FIX #24: we used to
# skip the project directory when sys.frozen was True (bundled), but
# many users put OPENAI_API_KEY only in the project .env and had
# silently-degraded cloud-mode with no feedback.
_config_dir = os.path.expanduser("~/.whisper-dictation")
_here = os.path.dirname(os.path.abspath(__file__))
_candidates = [
    os.path.join(_config_dir, ".env"),
    os.path.join(_here, ".env"),
    os.path.join(os.path.dirname(_here), ".env"),
    # Absolute path to the project source (for bundled apps that were
    # built from source and the user keeps .env only there)
    "/Users/maxsnigirev/Claude Code/Whispr Flow - Copy Cat/.env",
    "/Users/maxsnigirev/Claude Code/Whispr Flow - Copy Cat/whisper-dictation/.env",
]
for _p in _candidates:
    if os.path.isfile(_p):
        load_dotenv(_p, override=False)

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


def _generic_error_message(exc: Exception) -> str:
    """Map internal exceptions to a short, user-safe overlay message.

    Internal strings (which may contain API keys or tokens) are never
    shown verbatim — we pick a safe category.
    """
    msg = str(exc).lower()
    if "network" in msg or "connection" in msg or "timed out" in msg or "timeout" in msg:
        return "Network error"
    if "api" in msg and ("key" in msg or "401" in msg or "auth" in msg):
        return "API auth error"
    if "rate limit" in msg or "429" in msg:
        return "Rate limited"
    if "model" in msg:
        return "Model error"
    return "Processing failed"


ICON_IDLE = "🎙"
ICON_REC = "● REC"
ICON_PROCESSING = "⏳"


class WhisperDictationApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_IDLE, quit_button=None)

        # Ensure settings exist on disk
        S.load()

        self.recorder = Recorder(force_builtin=S.get("force_builtin_mic", True))

        # Single unified hotkey via CGEventTap (no subprocess, no multiprocessing)
        self.hotkey = FnKeyHandler(
            on_start=self._on_record_start,
            on_stop=self._on_record_stop,
        )
        # Flag used to abort in-flight transcription/cleanup on Escape
        self._cancel_flag = threading.Event()
        self._processing = False  # True while _process_audio is running
        self._recording_bundle_id = None  # captured at recording start
        self._mic_error_shown = False

        # Secondary hotkeys — Cmd+Shift+V re-pastes, Escape cancels.
        # Escape only fires cancel when we're actively recording or
        # processing — avoids triggering on every Escape keystroke
        # (bug #19).
        self.repaste_hotkey = RePasteHotkey(
            on_trigger=self._on_repaste,
            on_cancel=self._on_cancel,
            is_active=lambda: self.recorder.is_recording or self._processing,
        )

        self.overlay = StatusOverlay()
        self.recorder.set_level_callback(self.overlay.push_level)

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

    def _on_repaste(self) -> None:
        """Triggered by Cmd+Shift+V — re-paste the last transcription."""
        ok = _injector.repaste_last()
        if ok:
            self.overlay.show_done("↻ " + _injector.get_last_transcription())
        else:
            self.overlay.show_error("Nothing to re-paste")

    def _on_cancel(self) -> None:
        """Emergency cancel via Escape — reset all state.

        - Signals in-flight processing to bail out.
        - UI is reset IMMEDIATELY.
        - Heavy recorder teardown (stream.stop + WAV write + unlink) is
          dispatched to a background thread so Escape feels instant
          (bug fix #10).
        """
        log.info("Escape pressed — cancelling all operations")
        # 1. Flag in-flight processing to bail after the next step
        self._cancel_flag.set()

        # 2. Reset hotkey state IMMEDIATELY (thread-safe)
        try:
            self.hotkey.reset_state()
        except Exception as e:
            log.warning("hotkey reset_state failed: %s", e)

        # 3. Reset UI IMMEDIATELY
        self._set_title_safe(ICON_IDLE, "🔴 Start Recording")
        self.overlay.hide()

        # 4. Heavy recorder teardown in background
        def _tear_down():
            try:
                if self.recorder.is_recording:
                    audio_path = self.recorder.stop()
                    if audio_path:
                        try:
                            os.unlink(audio_path)
                        except OSError:
                            pass
            except Exception as e:
                log.warning("Cancel: recorder stop error: %s", e)
        threading.Thread(target=_tear_down, daemon=True).start()

    def _set_title_safe(self, title: str, record_item_title: str = None) -> None:
        """Update menu bar title on the main thread (AppKit is not thread-safe)."""
        from PyObjCTools import AppHelper
        def _do():
            try:
                self.title = title
                if record_item_title is not None:
                    self.record_item.title = record_item_title
            except Exception:
                pass
        AppHelper.callAfter(_do)

    def _on_record_start(self) -> None:
        # Capture bundle ID FIRST (before UI updates change focus)
        try:
            _, self._recording_bundle_id = get_focused_text_info()
        except Exception:
            self._recording_bundle_id = None

        # Start UI + recorder IMMEDIATELY — no blocking work before this.
        self.recorder.start()
        play_start()
        self._set_title_safe(ICON_REC, "⏹ Stop Recording")
        self.overlay.show_recording()

        # Streaming (if enabled) — open WebSocket in background thread so the
        # 200-500ms TCP/TLS handshake doesn't delay the sound + overlay.
        # Chunks are buffered in the streamer until the socket is ready.
        self._streamer = None
        if S.get("use_streaming", False) and S.get("mode", S.MODE_AUTO) != S.MODE_LOCAL:
            try:
                st = StreamingTranscriber()
                if st.start_async(sample_rate=16000):
                    self._streamer = st
                    self.recorder.set_chunk_callback(st.feed)
                    log.info("Streaming transcription enabled (connecting in background)")
            except Exception as e:
                log.warning("Streaming init failed, will use batch: %s", e)

        log.info("Recording started (app: %s)", self._recording_bundle_id)

    def _on_record_stop(self) -> None:
        audio_path = self.recorder.stop()
        play_stop()

        # Detach + close streaming session if we had one. Batch pipeline
        # can take over from recorded buffer if needed.
        self.recorder.set_chunk_callback(None)

        if audio_path is None:
            # Close any open streaming session — nothing to commit.
            streamer = getattr(self, "_streamer", None)
            if streamer is not None:
                try:
                    streamer.close()
                except Exception:
                    pass
                self._streamer = None

            self._set_title_safe(ICON_IDLE, "🔴 Start Recording")
            self.overlay.hide()

            # Recorder short-circuits on silent audio. Show a clear error
            # and a one-time notification directing the user to fix the
            # microphone permission.
            if getattr(self.recorder, "_last_error", None) == "mic_silent":
                log.warning("Recording was silent — mic permission likely missing")
                self.overlay.show_error("Check Microphone permission!")
                if not getattr(self, "_mic_error_shown", False):
                    self._mic_error_shown = True
                    rumps.notification(
                        title="Whisper Dictation",
                        subtitle="Microphone is silent",
                        message=(
                            "Grant mic access: System Settings → "
                            "Privacy & Security → Microphone → Whisper Dictation."
                        ),
                    )
            else:
                log.info("Recording too short, ignored")
            return

        self._set_title_safe(ICON_PROCESSING)
        self.overlay.show_processing()
        log.info("Processing audio: %s", audio_path)

        threading.Thread(target=self._process_audio, args=(audio_path,), daemon=True).start()

    def _process_audio(self, audio_path: str) -> None:
        """Full pipeline: VAD → transcribe → replace → clean → inject.

        Checks self._cancel_flag at each step so Escape can abort cleanly.
        """
        self._cancel_flag.clear()
        self._processing = True
        temp_files = [audio_path]

        def _was_cancelled() -> bool:
            if self._cancel_flag.is_set():
                log.info("Pipeline cancelled via Escape")
                self._reset_ui()
                self.overlay.hide()
                return True
            return False

        try:
            # Step 0: Detach streaming chunk callback (no more audio coming)
            self.recorder.set_chunk_callback(None)

            # Step 1: Streaming path — if a WebSocket session is active,
            # commit and wait briefly for the final transcript. On timeout
            # or error, silently fall back to batch using the same audio.
            raw_text = None
            streamer = getattr(self, "_streamer", None)
            if streamer is not None:
                try:
                    streamed = streamer.commit_and_wait(timeout=3.0)
                    if streamed:
                        from anti_hallucination import filter_transcription
                        raw_text = filter_transcription(streamed)
                        if raw_text:
                            log.info("Streaming transcription succeeded (%d chars)", len(raw_text))
                            # Record usage under streaming model in stats
                            try:
                                import stats as _stats
                                from transcriber import _audio_duration_seconds
                                dur = _audio_duration_seconds(audio_path)
                                if dur > 0:
                                    _stats.record_transcribe("gpt-4o-mini-transcribe", dur)
                            except Exception:
                                pass
                except Exception as e:
                    log.warning("Streaming commit failed, falling back to batch: %s", e)
                finally:
                    # Close in background — the TCP close handshake can
                    # take 1-2s and we don't need to wait for it before
                    # injecting the text.
                    _s = streamer
                    threading.Thread(
                        target=lambda: (_s.close() if _s else None),
                        daemon=True,
                    ).start()
                    self._streamer = None

            # Step 2: VAD (best-effort) — only if not already transcribed via streaming
            if raw_text is None and S.get("vad_enabled", True):
                vad_path = vad.strip_silence(audio_path)
                if _was_cancelled():
                    return
                if vad_path is None:
                    log.info("VAD: no speech detected — falling back to raw audio")
                elif vad_path != audio_path:
                    temp_files.append(vad_path)
                    audio_path = vad_path

            # Step 3: Batch transcribe (skipped if streaming already produced text)
            if raw_text is None:
                raw_text = transcribe(audio_path)
            if _was_cancelled():
                return
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

            if _was_cancelled():
                return

            # Step 5: Record stats + remember text for re-paste hotkey
            record_words(final_text)
            _injector.set_last_transcription(final_text)

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
            # BUG FIX #9: log full details for debugging, but show a
            # generic message in the overlay so API keys / tokens that
            # some exception strings contain don't leak on screen.
            log.error("Processing failed: %s", e, exc_info=True)
            generic = _generic_error_message(e)
            self.overlay.show_error(generic)
            self._reset_ui()
        finally:
            self._processing = False
            for p in temp_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _reset_ui(self) -> None:
        # BUG FIX #2: route AppKit updates through the main thread.
        # _reset_ui is called from worker threads (pipeline, cancel) and
        # setting rumps.App.title / MenuItem.title directly bridges to
        # NSStatusItem — not thread-safe.
        self._set_title_safe(ICON_IDLE, "🔴 Start Recording")

    # ── Menu handlers ───────────────────────────────────────────────────

    def _notify(self, title: str, message: str) -> None:
        rumps.notification(title="Whisper Dictation", subtitle=title, message=message)

    def _show_stats(self, _sender=None) -> None:
        u_today = get_usage_today()
        u_week = get_usage_week()
        u_month = get_usage_month()
        u_all = get_usage_all()

        def _fmt_usage(u):
            lines = []
            for row in u.get("by_model", []):
                model = row["model"]
                minutes = row["seconds"] / 60.0
                cost = row["cost_usd"]
                tag = "🆓" if cost == 0.0 else "💵"
                short = (
                    "gpt-4o-mini" if "gpt-4o" in model
                    else ("whisper-1" if model == "whisper-1" else "local")
                )
                lines.append(
                    f"    {tag} {short}: {minutes:.1f}m / {row['calls']} calls"
                    + (f" → ${cost:.4f}" if cost else " (free)")
                )
            if not lines:
                lines.append("    (no transcription yet)")

            gpt_tokens = u["gpt_input_tokens"] + u["gpt_output_tokens"]
            if gpt_tokens:
                lines.append(
                    f"    💵 GPT cleanup: {gpt_tokens:,} tokens"
                    f" ({u['gpt_input_tokens']:,} in / {u['gpt_output_tokens']:,} out)"
                    f" → ${u['gpt_cost_usd']:.4f}"
                )
            lines.append(f"    ──────────────")
            lines.append(f"    TOTAL: ${u['total_cost_usd']:.4f}")
            return "\n".join(lines)

        message = (
            f"📝 Words dictated\n"
            f"    Today:      {get_words_today():,}\n"
            f"    Last 7d:    {get_words_week():,}\n"
            f"    Last 30d:   {get_words_month():,}\n"
            f"\n"
            f"📊 Today\n{_fmt_usage(u_today)}\n"
            f"\n"
            f"📊 Last 7 days\n{_fmt_usage(u_week)}\n"
            f"\n"
            f"📊 Last 30 days\n{_fmt_usage(u_month)}\n"
            f"\n"
            f"📊 All time\n{_fmt_usage(u_all)}"
        )
        rumps.alert(title="📊 Dictation Statistics", message=message)

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
            "streaming <on|off>     (WebSocket Realtime API, faster UX, ~2.5x cost)\n"
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
                # BUG FIX #7: properly stop the old recorder before
                # replacing (prevents leaked InputStream + orphaned audio
                # callback thread).
                try:
                    if self.recorder.is_recording:
                        path = self.recorder.stop()
                        if path:
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                except Exception as e:
                    log.warning("old recorder stop failed: %s", e)
                self.recorder = Recorder(force_builtin=bool_map[value])
                # Re-attach the level callback on the new recorder
                self.recorder.set_level_callback(self.overlay.push_level)
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
        elif key == "streaming":
            if value in bool_map:
                S.set("use_streaming", bool_map[value])
                self._notify("Settings", f"Streaming: {value}")
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
        # BUG FIX #32: stop the overlay's NSTimer so the retain cycle
        # (timer → lambda → view → window) gets broken before exit.
        try:
            self.overlay.hide()
        except Exception:
            pass
        try:
            if self.recorder.is_recording:
                path = self.recorder.stop()
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except Exception:
            pass
        rumps.quit_application()


def main():
    app = WhisperDictationApp()

    # Install the unified hotkey tap on the main CFRunLoop before app.run()
    ok = app.hotkey.start()
    if not ok:
        log.error(
            "Failed to install hotkey tap. Grant Accessibility to "
            "Whisper Dictation.app in System Settings > Privacy & Security."
        )
    # Install re-paste hotkey (Cmd+Shift+V)
    app.repaste_hotkey.start()

    # TEMP: not switching to accessory — testing menu bar visibility
    # threading.Thread(target=_hide_from_dock_later, daemon=True).start()

    log.info("Whisper Dictation started (mode=%s, hotkey=%s).",
             S.get("mode"), S.get("hotkey"))
    app.run()


if __name__ == "__main__":
    main()
