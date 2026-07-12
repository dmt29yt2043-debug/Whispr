"""Dictation history — every transcription saved locally, recoverable from the menu.

Wispr Flow's history feature: if the paste landed in the wrong window (or
nowhere), the user opens the menu and copies the text back instead of
re-dictating a two-minute monologue.

Storage: SQLite at ~/.whisper-dictation/history.db, newest-first reads,
capped at _MAX_ENTRIES (oldest rows pruned on insert). Text is stored in
plaintext locally — same trade-off Wispr Flow makes; "Clear History" in
the menu wipes it.
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import List, Tuple

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.whisper-dictation")
_DB_PATH = os.path.join(_CONFIG_DIR, "history.db")

_MAX_ENTRIES = 200

_lock = threading.Lock()


@contextmanager
def _db():
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dictations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                text TEXT NOT NULL,
                app_bundle TEXT
            )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def add(text: str, app_bundle: str = None) -> None:
    """Record a dictation. Empty/whitespace text is ignored."""
    text = (text or "").strip()
    if not text:
        return
    try:
        with _lock, _db() as conn:
            conn.execute(
                "INSERT INTO dictations (text, app_bundle) VALUES (?, ?)",
                (text, app_bundle),
            )
            # Prune beyond the cap (keep newest _MAX_ENTRIES)
            conn.execute(
                """DELETE FROM dictations WHERE id NOT IN (
                       SELECT id FROM dictations ORDER BY id DESC LIMIT ?
                   )""",
                (_MAX_ENTRIES,),
            )
    except Exception as e:
        log.warning("History add failed: %s", e)


def get_recent(limit: int = 10) -> List[Tuple[int, str, str]]:
    """Return [(id, ts, text)] newest first."""
    try:
        with _lock, _db() as conn:
            rows = conn.execute(
                "SELECT id, ts, text FROM dictations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows
    except Exception as e:
        log.warning("History read failed: %s", e)
        return []


def clear() -> None:
    try:
        with _lock, _db() as conn:
            conn.execute("DELETE FROM dictations")
        log.info("History cleared")
    except Exception as e:
        log.warning("History clear failed: %s", e)


def count() -> int:
    try:
        with _lock, _db() as conn:
            return conn.execute("SELECT COUNT(*) FROM dictations").fetchone()[0]
    except Exception:
        return 0


def menu_title(text: str, max_chars: int = 48) -> str:
    """One-line preview for a menu item: collapse newlines, ellipsize."""
    one_line = " ".join((text or "").split())
    if len(one_line) <= max_chars:
        return one_line
    return one_line[: max_chars - 1].rstrip() + "…"
