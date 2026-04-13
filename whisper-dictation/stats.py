"""Statistics module — tracks word count per day in SQLite."""

import os
import sqlite3
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.whisper-dictation")
_DB_PATH = os.path.join(_CONFIG_DIR, "stats.db")


def _ensure_db() -> sqlite3.Connection:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS word_counts ("
        "  date TEXT PRIMARY KEY,"
        "  count INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.commit()
    return conn


def record_words(text: str) -> None:
    """Record word count for today."""
    if not text.strip():
        return
    word_count = len(text.split())
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO word_counts (date, count) VALUES (?, ?)"
            " ON CONFLICT(date) DO UPDATE SET count = count + ?",
            (today, word_count, word_count),
        )
        conn.commit()
        conn.close()
        log.info("Recorded %d words for %s", word_count, today)
    except Exception as e:
        log.error("Failed to record stats: %s", e)


def get_words_today() -> int:
    """Get word count for today."""
    today = datetime.now().strftime("%Y-%m-%d")
    return _get_words_since(today)


def get_words_week() -> int:
    """Get word count for the last 7 days."""
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    return _get_words_since(since)


def get_words_month() -> int:
    """Get word count for the last 30 days."""
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    return _get_words_since(since)


def _get_words_since(since_date: str) -> int:
    try:
        conn = _ensure_db()
        cursor = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM word_counts WHERE date >= ?",
            (since_date,),
        )
        result = cursor.fetchone()[0]
        conn.close()
        return result
    except Exception as e:
        log.error("Failed to get stats: %s", e)
        return 0
