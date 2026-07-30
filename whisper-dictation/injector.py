"""Text injection — copies text to clipboard and pastes via Cmd+V.

Features:
- AX focus check: if focused element isn't a text field, just copy to clipboard
- Clipboard restore (opt-in): saves previous clipboard, restores after paste
- FAIL-CLOSED pasting: Cmd+V is only pressed after the pasteboard is
  VERIFIED to hold our text (native NSPasteboard + changeCount); if the
  write can't be confirmed we never paste stale content
- Global inject lock: concurrent pipelines can't interleave copy/paste
- Post-paste clobber repair: if something rewrites the pasteboard within
  the async gap before the target app reads it, we re-assert our text
"""

import time
import logging
import threading
from typing import Optional, Tuple

import pyperclip
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    CGEventPost,
    kCGHIDEventTap,
    kCGEventFlagMaskCommand,
    kCGEventSourceUserData,
)

log = logging.getLogger(__name__)

V_KEY_CODE = 9  # 'V' on macOS

# Marker stamped into our synthetic Cmd+V events (eventSourceUserData).
# Our own CGEventTaps (repaste_hotkey) short-circuit marked events so the
# injected keystroke doesn't wait on a Python callback under load.
SYNTHETIC_EVENT_MARK = 0x57484953  # 'WHIS'

# Serializes the copy→verify→paste critical section across ALL callers
# (concurrent dictation pipelines, re-paste hotkey, history menu). Without
# it, a slow pipeline A and a fast pipeline B interleave: A copies A,
# B copies B, A pastes → A's target receives B. Observed live as "я диктую
# предложение, а вставляется предыдущее/чужое".
_INJECT_LOCK = threading.Lock()

# ── Native pasteboard (atomic, in-process, verifiable) ────────────────
#
# pyperclip shells out to pbcopy/pbpaste per call — slow (subprocess spawn)
# and unverifiable. NSPasteboard gives us changeCount: we know EXACTLY
# whether our write took and whether anyone clobbered it afterwards.

def _pb() :
    from AppKit import NSPasteboard
    return NSPasteboard.generalPasteboard()


def _pb_read() -> str:
    try:
        s = _pb().stringForType_("public.utf8-plain-text")
        return str(s) if s is not None else ""
    except Exception:
        try:
            return pyperclip.paste()
        except Exception:
            return ""


def _pb_write(text: str) -> Optional[int]:
    """Write text to the general pasteboard. Returns the new changeCount
    on verified success, None on failure. Never raises."""
    try:
        pb = _pb()
        pb.clearContents()
        ok = pb.setString_forType_(text, "public.utf8-plain-text")
        if not ok:
            return None
        count = pb.changeCount()
        # Read-back verification — belt and suspenders.
        if _pb_read() != text:
            return None
        return int(count)
    except Exception as e:
        log.debug("NSPasteboard write failed (%s) — pyperclip fallback", e)
        try:
            pyperclip.copy(text)
            deadline = time.time() + 0.5
            while time.time() < deadline:
                if _pb_read() == text:
                    return -1  # verified, changeCount unknown
                time.sleep(0.02)
        except Exception:
            pass
        return None


def _write_verified(text: str, attempts: int = 3) -> Optional[int]:
    """Write + verify with retries. None ⇒ could NOT confirm the clipboard
    holds our text — callers must NOT paste in that case."""
    for i in range(attempts):
        count = _pb_write(text)
        if count is not None:
            return count
        time.sleep(0.05 * (i + 1))
    return None

# How long to wait before putting the user's previous clipboard back.
#
# The Cmd+V we post is processed ASYNCHRONOUSLY by the target app — it
# reads the pasteboard whenever it gets around to handling the key event.
# Electron apps (Claude, ChatGPT, Slack) under load can take well over
# 600ms. The old 0.6s delay lost that race: we restored the OLD clipboard
# before the app read the NEW text, so the app pasted stale clipboard
# content instead of the dictation. 2s comfortably covers slow apps; the
# restore is still skipped entirely if the user copied something newer.
_RESTORE_DELAY_SEC = 2.0

# Module-level cache of the last transcription so the re-paste hotkey
# can recover text if the user's cursor was off-target at paste time.
_last_transcription: str = ""


def get_last_transcription() -> str:
    """Return the most recent transcription (may be empty)."""
    return _last_transcription


def set_last_transcription(text: str) -> None:
    global _last_transcription
    _last_transcription = text or ""


def _press_cmd_v() -> None:
    """Simulate Cmd+V via Quartz CGEvents (marked as ours)."""
    down = CGEventCreateKeyboardEvent(None, V_KEY_CODE, True)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventSetIntegerValueField(down, kCGEventSourceUserData, SYNTHETIC_EVENT_MARK)
    CGEventPost(kCGHIDEventTap, down)

    up = CGEventCreateKeyboardEvent(None, V_KEY_CODE, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventSetIntegerValueField(up, kCGEventSourceUserData, SYNTHETIC_EVENT_MARK)
    CGEventPost(kCGHIDEventTap, up)


# Bumped at every paste; background clobber-guards stop the moment a newer
# paste starts, so an old guard can never fight a new dictation for the
# pasteboard.
_paste_generation = 0


def _copy_verify_paste(text: str) -> bool:
    """The fail-closed core: verified copy → Cmd+V → clobber guard.

    Runs under _INJECT_LOCK. Returns True iff Cmd+V was actually sent
    with OUR text confirmed on the pasteboard. On any verification
    failure the paste is NOT sent — pasting stale/foreign clipboard
    content is strictly worse than pasting nothing (the text stays
    available in the clipboard-retry, history and Cmd+Shift+V).
    """
    global _paste_generation
    _paste_generation += 1
    my_gen = _paste_generation

    count = _write_verified(text)
    if count is None:
        log.error("Clipboard write could NOT be verified — NOT pasting "
                  "(%d chars kept in history)", len(text))
        return False

    # Final instant re-check right before the keystroke: nobody clobbered
    # the pasteboard between write and now.
    if _pb_read() != text:
        count = _write_verified(text)
        if count is None:
            log.error("Pasteboard clobbered and re-write failed — NOT pasting")
            return False

    _press_cmd_v()

    # Short synchronous guard: most apps read the pasteboard well within
    # 250ms of the keystroke. Held under the lock so a concurrent inject
    # can't slip its copy into this window.
    deadline = time.time() + 0.25
    while time.time() < deadline:
        time.sleep(0.05)
        try:
            if _pb_read() != text:
                log.warning("Pasteboard clobbered right after paste — re-asserting")
                _write_verified(text, attempts=2)
        except Exception:
            break

    # Extended watch runs in the background — LOG-ONLY. Past the 250ms
    # sync window a clipboard change may be the USER's own Cmd+C or our
    # restore thread, which we must never overwrite; the dangerous
    # cross-pipeline race is already eliminated by _INJECT_LOCK. Logging
    # keeps visibility into third-party clobberers (clipboard managers).
    def _watch():
        g_deadline = time.time() + 1.25
        while time.time() < g_deadline:
            time.sleep(0.15)
            if _paste_generation != my_gen:
                return  # a newer paste owns the pasteboard now
            try:
                if _pb_read() != text:
                    log.info("Pasteboard changed within 1.5s of paste "
                             "(user copy / restore / clipboard manager)")
                    return
            except Exception:
                return
    threading.Thread(target=_watch, daemon=True).start()
    return True


def repaste_last(restore_clipboard: bool = True) -> bool:
    """Paste the last transcription again (Cmd+Shift+V / history menu).

    Same fail-closed core as inject_text. With restore_clipboard the
    previous clipboard returns after _RESTORE_DELAY_SEC unless the user
    copied something newer meanwhile.

    Returns True if a paste was actually sent.
    """
    if not _last_transcription:
        log.info("Re-paste requested but no previous transcription")
        return False
    text = _last_transcription

    prev_clipboard: Optional[str] = None
    if restore_clipboard:
        prev_clipboard = _pb_read() or None

    with _INJECT_LOCK:
        ok = _copy_verify_paste(text)
    if not ok:
        return False
    log.info("Re-pasted last transcription (%d chars)", len(text))

    if restore_clipboard and prev_clipboard is not None:
        def _restore():
            time.sleep(_RESTORE_DELAY_SEC)
            try:
                # Under the inject lock so we can't interleave with a new
                # dictation's copy→verify→paste critical section.
                with _INJECT_LOCK:
                    if _pb_read() == text:
                        _write_verified(prev_clipboard, attempts=1)
                        log.info("Clipboard restored after re-paste (%.1fs)", _RESTORE_DELAY_SEC)
                    else:
                        log.info("Clipboard changed by user — skipping restore")
            except Exception:
                pass
        threading.Thread(target=_restore, daemon=True).start()
    return True


def inject_text(
    text: str,
    check_focus: bool = True,
    restore_clipboard: bool = True,
) -> str:
    """Paste text into the focused app, or copy to clipboard if no text focus.

    Returns "pasted" | "copied" | "skipped" | "failed".
    "failed" ⇒ the clipboard write could not be verified, so NO Cmd+V was
    sent (never paste unverified/stale content). Callers should surface
    that instead of pretending success.
    """
    if not text:
        return "skipped"

    # Save previous clipboard (opt-in restore mode only)
    prev_clipboard: Optional[str] = None
    if restore_clipboard:
        prev_clipboard = _pb_read() or None

    # Check focus
    can_paste = True
    if check_focus:
        try:
            from focus_check import get_focused_text_info
            has_text_focus, _bundle_id = get_focused_text_info()
            can_paste = has_text_focus
        except Exception as e:
            log.debug("focus check error: %s (allowing paste)", e)

    if can_paste:
        with _INJECT_LOCK:
            ok = _copy_verify_paste(text)
        if not ok:
            return "failed"
        log.info("Injected %d chars into focused app", len(text))
        result = "pasted"

        if restore_clipboard and prev_clipboard is not None:
            injected = text  # capture for closure
            def _restore():
                time.sleep(_RESTORE_DELAY_SEC)
                try:
                    # Under the inject lock: can't interleave with a new
                    # dictation's copy→verify→paste critical section.
                    with _INJECT_LOCK:
                        if _pb_read() == injected:
                            _write_verified(prev_clipboard, attempts=1)
                            log.info("Clipboard restored (%.1fs after paste)", _RESTORE_DELAY_SEC)
                        else:
                            log.info("Clipboard was changed by user — skipping restore")
                except Exception:
                    pass
            threading.Thread(target=_restore, daemon=True).start()
    else:
        with _INJECT_LOCK:
            if _write_verified(text) is None:
                return "failed"
        log.info("No text focus, copied %d chars to clipboard", len(text))
        result = "copied"

    return result
