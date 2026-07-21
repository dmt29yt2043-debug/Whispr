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


# Menu-bar STATE markers. These are passed to _set_title_safe(); it maps
# them to either a PNG icon (when our custom-rendered mic is available)
# or the plain emoji/text fallback below. The actual values are arbitrary
# sentinel strings; existing call sites pass these constants unchanged.
ICON_IDLE = "🎙"        # also the emoji fallback if PNG generation fails
ICON_REC = "● REC"
ICON_PROCESSING = "⏳"

# Real paths get filled in __init__ via mic_icon.ensure_*; menu-bar
# state transitions use _set_title_safe() to swap path/title atomically.
# Idle comes in TWO colour variants — the app picks per system appearance
# (template images render blank in the status item on this macOS build,
# so auto-tinting is off the table; the coloured REC icon always worked).
_MENU_BAR_ICON_IDLE_WHITE: str = ""
_MENU_BAR_ICON_IDLE_BLACK: str = ""
_MENU_BAR_ICON_REC: str = ""


def _menu_bar_is_dark() -> bool:
    """True if the menu bar renders light-on-dark (needs the white glyph)."""
    try:
        from AppKit import NSApplication
        ap = NSApplication.sharedApplication().effectiveAppearance()
        name = ap.bestMatchFromAppearancesWithNames_(
            ["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"])
        return name == "NSAppearanceNameDarkAqua"
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip().lower() == "dark"
    except Exception:
        # Most macOS users (and this one) run dark mode — white is the
        # safer blind default.
        return True


def _current_idle_icon() -> str:
    """Pick the idle icon variant matching the current appearance."""
    if _menu_bar_is_dark():
        return _MENU_BAR_ICON_IDLE_WHITE or _MENU_BAR_ICON_IDLE_BLACK
    return _MENU_BAR_ICON_IDLE_BLACK or _MENU_BAR_ICON_IDLE_WHITE


class WhisperDictationApp(rumps.App):
    def __init__(self):
        # Generate the custom mic icons BEFORE super().__init__ — rumps reads
        # the icon argument immediately when constructing the status item.
        global _MENU_BAR_ICON_IDLE_WHITE, _MENU_BAR_ICON_IDLE_BLACK, _MENU_BAR_ICON_REC
        try:
            import mic_icon
            _MENU_BAR_ICON_IDLE_WHITE = mic_icon.ensure_menu_bar_icon(dark_menu_bar=True) or ""
            _MENU_BAR_ICON_IDLE_BLACK = mic_icon.ensure_menu_bar_icon(dark_menu_bar=False) or ""
            _MENU_BAR_ICON_REC = mic_icon.ensure_menu_bar_icon_recording() or ""
        except Exception as e:
            log.warning("Custom mic icon generation failed: %s — falling back to emoji", e)

        idle_icon = _current_idle_icon()
        if idle_icon:
            # Coloured icon, template=False — template rendering produces a
            # blank status item on this macOS build (see _menu_bar_is_dark).
            super().__init__(
                name="Whisper Dictation",
                title=None,
                icon=idle_icon,
                template=False,
                quit_button=None,
            )
        else:
            super().__init__(ICON_IDLE, quit_button=None)

        # Last menu-bar STATE (one of the ICON_* constants) — replayed by
        # _reassert_menu_icon() after display/theme changes, resolving the
        # correct icon variant at replay time.
        self._last_bar_title = ICON_IDLE

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
        # One-click recovery: cursor landed in the wrong field → open the
        # menu, click this, Cmd+V where the text SHOULD have gone.
        self.copy_last_item = rumps.MenuItem(
            "📋 Copy Last Dictation", callback=self._copy_last_dictation)
        self.history_item = rumps.MenuItem("🕘 Recent Dictations")
        self.stats_item = rumps.MenuItem("📊 Statistics", callback=self._show_stats)
        self.replacements_item = rumps.MenuItem("🔄 Text Replacements", callback=self._show_replacements)
        self.dictionary_item = rumps.MenuItem("📖 Add Word to Dictionary…", callback=self._add_dictionary_word)
        self.settings_item = rumps.MenuItem("⚙️ Settings", callback=self._show_settings)
        self.quit_item = rumps.MenuItem("Quit Whisper Dictation", callback=self._quit)

        self.menu = [
            self.record_item,
            self.copy_last_item,
            rumps.separator,
            self.history_item,
            self.stats_item,
            self.replacements_item,
            self.dictionary_item,
            self.settings_item,
            rumps.separator,
            self.quit_item,
        ]
        # Populate the history submenu with what's already on disk so
        # past dictations survive app restarts.
        self._refresh_history_menu()

        # Warmup local model in background if needed
        if S.get("mode") in (S.MODE_LOCAL, S.MODE_AUTO):
            threading.Thread(target=warmup_local_model, daemon=True).start()

        # Listen for macOS wake-from-sleep so we can heal a stale PortAudio
        # handle BEFORE the user hits the hotkey. Without this, the first
        # recording after wake silently captures zeros for ~13s (peak=0.0,
        # rms=0.0) — InputStream opens fine, callback fires, but CoreAudio
        # delivers an empty buffer because the HAL Audio Unit died during
        # sleep. The fix is sd._terminate()+sd._initialize() which forces
        # PortAudio to rebuild the bridge. Done lazily inside recorder
        # start() (mark_subsystem_dirty just sets a flag) so we don't
        # interfere if a recording is somehow already running at wake.
        self._install_wake_observer()

        # Re-assert the menu-bar icon after monitor plug/unplug or wake —
        # macOS occasionally drops status-item images during display
        # reconfiguration, which users saw as "the icon disappeared".
        self._install_screen_change_observer()

        # Swap the white/black idle glyph when the user toggles light/dark
        # mode (we can't use template auto-tinting — it renders blank).
        self._install_theme_change_observer()

        # Surface API outages (quota exhausted) as a one-shot notification
        # rather than letting the user wait through 20s of silent retries.
        try:
            import api_status
            api_status.set_notify_callback(self._on_api_breaker_tripped)
        except Exception as e:
            log.debug("api_status notify hook failed: %s", e)

    def _on_api_breaker_tripped(self, reason: str) -> None:
        """Called once when the OpenAI API circuit breaker trips.

        Tells the user we're falling back to the local model. Reason
        usually contains 'insufficient_quota' / billing wording, which
        we surface so they know to top up.
        """
        short = reason
        if "insufficient_quota" in reason.lower() or "exceeded" in reason.lower():
            short = "OpenAI quota exhausted — using local model. Check billing."
        elif "billing" in reason.lower():
            short = "OpenAI billing issue — using local model."
        self._notify("API issue", short)

    def _install_wake_observer(self) -> None:
        """Subscribe to NSWorkspaceDidWakeNotification on the shared workspace.

        Best-effort — if AppKit isn't importable we just skip and rely on
        the post-silent-recording self-heal as a backstop.
        """
        try:
            from AppKit import NSWorkspace
            from Foundation import NSObject
            recorder = self.recorder

            outer = self

            class _WakeObserver(NSObject):
                def wakeFromSleep_(self, _notification):
                    recorder.mark_subsystem_dirty(reason="wake-from-sleep")
                    # Wake often comes with a display-config change (lid
                    # opened, dock reconnected) — refresh the menu icon too.
                    outer._reassert_menu_icon(delay=2.0)

            self._wake_observer = _WakeObserver.alloc().init()
            nc = NSWorkspace.sharedWorkspace().notificationCenter()
            nc.addObserver_selector_name_object_(
                self._wake_observer,
                "wakeFromSleep:",
                "NSWorkspaceDidWakeNotification",
                None,
            )
            log.info("Wake-from-sleep observer installed")
        except Exception as e:
            log.warning(
                "Could not install wake observer (%s) — relying on "
                "silent-recording auto-heal instead", e,
            )

    # ── Recording lifecycle ─────────────────────────────────────────────

    def _toggle_recording(self, sender) -> None:
        if self.recorder.is_recording:
            self._on_record_stop()
        else:
            self._on_record_start()

    def _refresh_history_menu(self) -> None:
        """Rebuild the Recent Dictations submenu (last 10 + Clear).

        Clicking an entry copies the FULL text back to the clipboard — the
        recovery path for "pasted into the wrong window": open the menu,
        click the dictation, Cmd+V where it should have gone. Safe to call
        from any thread; menu mutation is marshalled to the main thread.
        """
        import history

        entries = history.get_recent(limit=10)

        def _do():
            try:
                try:
                    self.history_item.clear()
                except AttributeError:
                    # First call: the MenuItem has no NSMenu yet — .add()
                    # below will create it.
                    pass
                if not entries:
                    empty = rumps.MenuItem("(no dictations yet)")
                    empty.set_callback(None)
                    self.history_item.add(empty)
                else:
                    for _id, ts, text in entries:
                        title = history.menu_title(text)
                        item = rumps.MenuItem(
                            title, callback=self._copy_history_entry)
                        # Stash the full text on the item — callback reads it.
                        item._full_text = text
                        self.history_item.add(item)
                self.history_item.add(rumps.separator)
                self.history_item.add(
                    rumps.MenuItem("Clear History", callback=self._clear_history))
            except Exception as e:
                log.warning("History menu refresh failed: %s", e)

        from PyObjCTools import AppHelper
        AppHelper.callAfter(_do)

    def _copy_last_dictation(self, _sender=None) -> None:
        """Menu: put the most recent dictation back on the clipboard."""
        text = _injector.get_last_transcription()
        if not text:
            # App may have restarted since the dictation — fall back to history
            try:
                import history
                rows = history.get_recent(limit=1)
                text = rows[0][2] if rows else ""
            except Exception:
                text = ""
        if not text:
            self._notify("Nothing to copy", "No dictations yet")
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            _injector.set_last_transcription(text)
            preview = text[:80] + "…" if len(text) > 80 else text
            self._notify("Copied — press Cmd+V to paste", preview)
        except Exception as e:
            log.warning("Copy last dictation failed: %s", e)

    def _copy_history_entry(self, sender) -> None:
        text = getattr(sender, "_full_text", "") or sender.title
        try:
            import pyperclip
            pyperclip.copy(text)
            # Also make it the re-paste target so Cmd+Shift+V works too.
            _injector.set_last_transcription(text)
            preview = text[:80] + "…" if len(text) > 80 else text
            self._notify("Copied to clipboard", preview)
        except Exception as e:
            log.warning("History copy failed: %s", e)

    def _clear_history(self, _sender=None) -> None:
        import history
        history.clear()
        self._refresh_history_menu()
        self._notify("History", "Dictation history cleared")

    def _add_dictionary_word(self, _sender=None) -> None:
        """Menu: add a term to the personal dictionary.

        Terms bias the transcription decoder (batch prompt) and the GPT
        cleanup pass toward the user's exact spellings — names, brands,
        project jargon ("Whispr Flow", "RIZY", …).
        """
        import dictionary
        current = dictionary.get_terms()
        preview = ", ".join(current[-8:]) if current else "(empty)"
        w = rumps.Window(
            message=(
                "Add a name/brand/term the transcriber should spell "
                "exactly.\nCurrent dictionary (%d): %s" % (len(current), preview)
            ),
            title="Personal Dictionary",
            default_text="", ok="Add", cancel="Cancel",
            dimensions=(300, 25),
        )
        r = w.run()
        if not r.clicked:
            return
        term = r.text.strip()
        if not term:
            return
        if dictionary.add_term(term):
            self._notify("Dictionary", f"Added: {term}")
        else:
            self._notify("Dictionary", f"Already in dictionary: {term}")

    def _on_repaste(self) -> None:
        """Triggered by Cmd+Shift+V — re-paste the last transcription."""
        ok = _injector.repaste_last(
            restore_clipboard=S.get("restore_clipboard", False))
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
        """Update menu bar title on the main thread (AppKit is not thread-safe).

        If our custom PNG icons are available, we ALSO swap the menu-bar
        glyph between the idle template (auto-tinted black/white) and a
        solid-red REC variant. That way the recording state is obvious
        regardless of the menu bar appearance.
        """
        from PyObjCTools import AppHelper

        # Remember the STATE (not the resolved path) so a later re-assert
        # picks the right icon variant for the theme at that moment.
        self._last_bar_title = title

        # Decide which icon path goes with this title — based on the
        # status string the existing callers use. All icons are coloured
        # with template=False: template images render blank in the status
        # item on this macOS build.
        idle_icon = _current_idle_icon()
        if title == ICON_REC and _MENU_BAR_ICON_REC:
            target_icon, target_template = _MENU_BAR_ICON_REC, False
            target_title = None  # icon alone, no "● REC" text — keeps the bar tidy
        elif title == ICON_IDLE and idle_icon:
            target_icon, target_template = idle_icon, False
            target_title = None
        elif title == ICON_PROCESSING and idle_icon:
            # Keep the mic glyph and show the hourglass NEXT to it.
            # Never set icon=None here: a status item whose icon
            # assignment later fails while title is also empty renders
            # ZERO-width — i.e. the icon "vanishes" from the menu bar.
            target_icon, target_template = idle_icon, False
            target_title = title
        else:
            # No custom icons available — fall back to text/emoji only
            target_icon, target_template = None, None
            target_title = title

        def _do():
            try:
                if target_icon is not None:
                    # rumps wraps a NSStatusItem; .icon and .template both
                    # forward to the underlying button.image / .template.
                    self.icon = target_icon
                    try:
                        self.template = target_template
                    except Exception:
                        pass
                    self.title = target_title  # clears any leftover text
                else:
                    self.icon = None
                    self.title = target_title
                if record_item_title is not None:
                    self.record_item.title = record_item_title
            except Exception:
                pass
        AppHelper.callAfter(_do)

    def _reassert_menu_icon(self, delay: float = 1.0) -> None:
        """Re-apply the last menu-bar state after a display/theme change.

        When a monitor is plugged/unplugged, the Mac wakes into a different
        display configuration, or the user switches light/dark mode, macOS
        rebuilds the menu bar and occasionally drops or blanks status-item
        images. Replaying the last state through _set_title_safe resolves
        the correct icon variant for the CURRENT theme and forces a redraw.
        """
        title = getattr(self, "_last_bar_title", None)
        if title is None:
            return

        def _later():
            import time as _t
            _t.sleep(delay)
            self._set_title_safe(title)

        threading.Thread(target=_later, daemon=True).start()

    def _install_theme_change_observer(self) -> None:
        """Re-pick the idle icon colour when light/dark mode changes."""
        try:
            from Foundation import NSObject, NSDistributedNotificationCenter
            outer = self

            class _ThemeObserver(NSObject):
                def themeChanged_(self, _note):
                    log.info("System theme changed — re-picking menu bar icon")
                    outer._reassert_menu_icon(delay=0.8)

            self._theme_observer = _ThemeObserver.alloc().init()
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._theme_observer,
                "themeChanged:",
                "AppleInterfaceThemeChangedNotification",
                None,
            )
            log.info("Theme-change observer installed")
        except Exception as e:
            log.warning("Could not install theme observer: %s", e)

    def _install_screen_change_observer(self) -> None:
        """Re-assert the menu-bar icon whenever displays are reconfigured.

        NSApplicationDidChangeScreenParametersNotification fires on monitor
        connect/disconnect, resolution change, and wake-with-different-
        displays — exactly the moments users reported the icon vanishing.
        """
        try:
            from Foundation import NSObject, NSNotificationCenter
            outer = self

            class _ScreenObserver(NSObject):
                def screensChanged_(self, _note):
                    log.info("Screen parameters changed — re-asserting menu bar icon")
                    outer._reassert_menu_icon(delay=1.5)

            self._screen_observer = _ScreenObserver.alloc().init()
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._screen_observer,
                "screensChanged:",
                "NSApplicationDidChangeScreenParametersNotification",
                None,
            )
            log.info("Screen-change observer installed")
        except Exception as e:
            log.warning("Could not install screen-change observer: %s", e)

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
        self.recorder.set_stream_stopped_callback(None)
        if S.get("use_streaming", False) and S.get("mode", S.MODE_AUTO) != S.MODE_LOCAL:
            try:
                st = StreamingTranscriber()
                if st.start_async(sample_rate=24000):
                    self._streamer = st
                    self.recorder.set_chunk_callback(st.feed)
                    # Commit the instant the input stream closes — the
                    # server transcribes the tail while we write the WAV.
                    self.recorder.set_stream_stopped_callback(st.commit_async)
                    log.info("Streaming transcription enabled (connecting in background)")
            except Exception as e:
                log.warning("Streaming init failed, will use batch: %s", e)

        # Pre-warm the HTTPS connection to api.openai.com WHILE the user is
        # speaking. By release time the pool has a live socket, so the
        # transcription POST skips the TCP+TLS handshake. Throttled inside.
        if S.get("mode", S.MODE_AUTO) != S.MODE_LOCAL:
            try:
                import transcriber as _tr
                threading.Thread(target=_tr.prewarm_connection, daemon=True).start()
            except Exception:
                pass

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
            streamed_duration = 0.0
            # GA Realtime (gpt-realtime-whisper) streams transcript deltas
            # during recording and accumulates them, so ANY recording length
            # is safe — the old 8s cap existed for the retired beta API
            # whose gpt-4o models dropped everything after the first pause.
            # The chars/sec sanity check below stays as the safety net:
            # a truncated-looking result still falls back to batch.
            _STREAMING_MAX_SECONDS = 600.0
            try:
                from transcriber import _audio_duration_seconds
                audio_duration = _audio_duration_seconds(audio_path)
            except Exception:
                audio_duration = 0.0

            streamer = getattr(self, "_streamer", None)
            if streamer is not None and 0 < audio_duration <= _STREAMING_MAX_SECONDS:
                try:
                    import time as _t
                    _t0 = _t.time()
                    streamed = streamer.commit_and_wait(timeout=6.0)
                    if streamed:
                        from anti_hallucination import filter_transcription
                        raw_text = filter_transcription(streamed)
                        if raw_text:
                            chars_per_sec = len(raw_text) / audio_duration
                            if chars_per_sec < 3.0:
                                # Looks truncated — batch it
                                log.warning(
                                    "Streaming gave %d chars for %.2fs (%.1f c/s) — "
                                    "likely truncated, falling back to batch",
                                    len(raw_text), audio_duration, chars_per_sec,
                                )
                                raw_text = None
                            else:
                                streamed_duration = audio_duration
                                log.info(
                                    "Streaming transcription succeeded "
                                    "(%d chars for %.1fs audio, wait %.2fs)",
                                    len(raw_text), audio_duration, _t.time() - _t0)
                                try:
                                    import stats as _stats
                                    _stats.record_transcribe(
                                        "gpt-realtime-whisper", audio_duration)
                                except Exception:
                                    pass
                except Exception as e:
                    log.warning("Streaming commit failed, falling back to batch: %s", e)
            elif streamer is not None:
                log.info("Audio %.1fs > %.1fs — skipping streaming, using batch",
                         audio_duration, _STREAMING_MAX_SECONDS)

            # Close any streaming session in background (both paths) — the
            # TCP close handshake can take 1-2s and we don't need to wait.
            if streamer is not None:
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

            # Step 3a: Voice snippets — if the whole dictation is a snippet
            # trigger ("моя подпись"), paste the template and skip cleanup.
            import snippets as _snippets
            snippet_text = _snippets.expand(raw_text)
            if snippet_text is not None:
                final_text = snippet_text
            else:
                # Step 3b: Text replacements (exact-match overrides cleanup)
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
            # History: every dictation is recoverable from the menu even
            # if the paste lands in the wrong window.
            try:
                import history
                history.add(final_text, app_bundle=self._recording_bundle_id)
                self._refresh_history_menu()
            except Exception as e:
                log.debug("history record failed: %s", e)

            # Step 6: Inject with focus check + clipboard restore
            result = inject_text(
                final_text,
                check_focus=S.get("check_focus", True),
                restore_clipboard=S.get("restore_clipboard", False),
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


def _acquire_singleton_lock():
    """Exit if another instance is already running.

    The app can be started two ways — the LaunchAgent (primary, python3
    directly) and the /Applications bundle (Launchpad convenience). If
    both run, two hotkey taps and two recorders fight over the mic. An
    exclusive flock on a lockfile is atomic and self-releasing on process
    death. Returns the open file object (must stay referenced for the
    lifetime of the process).
    """
    import fcntl
    lock_path = os.path.expanduser("~/.whisper-dictation/app.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Another instance is already running — exiting")
        sys.exit(0)
    f.write(str(os.getpid()))
    f.flush()
    return f


def main():
    global _singleton_lock
    _singleton_lock = _acquire_singleton_lock()

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

    # Rescue the status item from off-screen parking BEFORE it's created.
    # macOS persists a per-app "preferred position" for every status item.
    # On this machine the window server had parked our item beyond the
    # left edge of the external display (NSStatusBarWindow frame probe
    # showed x=-3924) — the item existed, isVisible()=True, but was
    # outside every screen, so no icon/template/activation-policy fix
    # could ever make it show. Writing a sane distance-from-right-edge
    # before creation makes the window server place it inside the bar.
    # Only write when the stored value is missing or absurd, so a manual
    # ⌘-drag by the user is respected afterwards.
    # IMPORTANT domain subtlety, learned the hard way: our process is
    # python3 (the bundle's launcher execs it), so NSUserDefaults reads
    # com.apple.python3 — but the WINDOW SERVER, when the app is launched
    # via the bundle, keys the item position on the BUNDLE's domain
    # (com.snigirev.whisper-dictation). With that domain empty, new items
    # are appended at the LEFT END of the global status row, which on a
    # multi-display setup lands beyond the leftmost display edge —
    # invisible on every screen. Fix: seed the position in BOTH domains.
    try:
        from Foundation import NSUserDefaults
        # Seed positions for BOTH the default identity (Item-0) and the
        # fresh identity we rename to after startup (WhisprMic).
        _pos_keys = (
            "NSStatusItem Preferred Position Item-0",
            "NSStatusItem Preferred Position WhisprMic",
        )
        for _pos_key in _pos_keys:
            for _domain in (None, "com.snigirev.whisper-dictation", "com.apple.python3"):
                try:
                    if _domain is None:
                        _ud = NSUserDefaults.standardUserDefaults()
                        _cur = _ud.objectForKey_(_pos_key)
                    else:
                        _ud = NSUserDefaults.alloc().initWithSuiteName_(None)
                        _pd = _ud.persistentDomainForName_(_domain) or {}
                        _cur = _pd.get(_pos_key)
                    _ok = _cur is not None and 10.0 <= float(_cur) <= 1500.0
                except Exception:
                    _ok = False
                if not _ok:
                    try:
                        if _domain is None:
                            _ud.setFloat_forKey_(300.0, _pos_key)
                            _ud.synchronize()
                        else:
                            _pd = dict(_ud.persistentDomainForName_(_domain) or {})
                            _pd[_pos_key] = 300.0
                            _ud.setPersistentDomain_forName_(_pd, _domain)
                        log.info("Seeded %r=300 in %s (was %r)",
                                 _pos_key, _domain or "standard", _cur)
                    except Exception as e:
                        log.warning("Position seed failed for %s: %s", _domain, e)
    except Exception as e:
        log.warning("Could not ensure status item position: %s", e)

    # Dock-icon hiding MUST happen AFTER the status item exists.
    # Empirical fact on macOS Tahoe, verified via CGWindowList: an
    # NSStatusItem created while the app is in Accessory mode never gets
    # a window — `Python items: []` — the menu bar icon simply does not
    # exist. Created in Regular mode it works, and it SURVIVES a later
    # flip to Accessory. So: launch Regular (Dock shows Python for ~3s),
    # flip, then re-assert the icon as belt-and-suspenders.
    def _flip_to_accessory_later():
        import time as _t
        _t.sleep(3.0)
        # Detach the status item from the cursed "Item-0" identity.
        # macOS keys per-item state (position AND Tahoe's hidden/parked
        # flag) on (app, autosaveName). Whatever hid Item-0 for the
        # bundle identity survives every restart — but a FRESH autosave
        # name starts clean, and we pre-seeded its preferred position.
        try:
            from PyObjCTools import AppHelper
            item = getattr(app._nsapp, "nsstatusitem", None)
            if item is not None:
                def _rename():
                    try:
                        item.setAutosaveName_("WhisprMic")
                        item.setVisible_(True)
                        log.info("Status item autosave renamed to WhisprMic, visible=%s",
                                 bool(item.isVisible()))
                    except Exception as e:
                        log.warning("Status item rename failed: %s", e)
                AppHelper.callAfter(_rename)
        except Exception as e:
            log.warning("Status item rescue failed: %s", e)
        _t.sleep(0.5)
        try:
            from AppKit import NSApplication
            from PyObjCTools import AppHelper
            AppHelper.callAfter(
                lambda: NSApplication.sharedApplication().setActivationPolicy_(1))
            log.info("Activation policy flipped to Accessory (post status-item)")
        except Exception as e:
            log.warning("Accessory flip failed: %s", e)
        _t.sleep(1.0)
        app._reassert_menu_icon(delay=0.5)

    threading.Thread(target=_flip_to_accessory_later, daemon=True).start()

    log.info("Whisper Dictation started (mode=%s, hotkey=%s).",
             S.get("mode"), S.get("hotkey"))
    app.run()


if __name__ == "__main__":
    main()
