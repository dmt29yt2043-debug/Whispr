"""Statistics — word count + OpenAI API usage (seconds/tokens) + costs in SQLite."""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.whisper-dictation")
_DB_PATH = os.path.join(_CONFIG_DIR, "stats.db")

# OpenAI pricing (USD) — update here if rates change
# gpt-4o-mini-transcribe: $0.003 per minute (we use this first)
# whisper-1: $0.006 per minute (fallback)
# We report an average; actual $ per request may vary by model used.
PRICE_WHISPER_PER_SEC = 0.003 / 60.0

# GPT-4o-mini: $0.15 per 1M input tokens, $0.60 per 1M output tokens
PRICE_GPT4O_MINI_INPUT_PER_TOKEN = 0.15 / 1_000_000
PRICE_GPT4O_MINI_OUTPUT_PER_TOKEN = 0.60 / 1_000_000


def _ensure_db() -> sqlite3.Connection:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS word_counts ("
        "  date TEXT PRIMARY KEY,"
        "  count INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage ("
        "  date TEXT PRIMARY KEY,"
        "  whisper_seconds REAL NOT NULL DEFAULT 0,"
        "  gpt_input_tokens INTEGER NOT NULL DEFAULT 0,"
        "  gpt_output_tokens INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.commit()
    return conn


# ── Words ───────────────────────────────────────────────────────────

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
    return _get_words_since(datetime.now().strftime("%Y-%m-%d"))


def get_words_week() -> int:
    return _get_words_since((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))


def get_words_month() -> int:
    return _get_words_since((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))


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


# ── Usage (seconds / tokens) ────────────────────────────────────────

def record_whisper_seconds(seconds: float) -> None:
    """Record audio sent to the Whisper API (for cost tracking)."""
    if seconds <= 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO usage (date, whisper_seconds) VALUES (?, ?)"
            " ON CONFLICT(date) DO UPDATE SET whisper_seconds = whisper_seconds + ?",
            (today, seconds, seconds),
        )
        conn.commit()
        conn.close()
        log.info("Recorded %.2fs of Whisper API usage", seconds)
    except Exception as e:
        log.error("Failed to record whisper usage: %s", e)


def record_gpt_tokens(input_tokens: int, output_tokens: int) -> None:
    """Record GPT-4o-mini token usage."""
    if input_tokens <= 0 and output_tokens <= 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO usage (date, gpt_input_tokens, gpt_output_tokens) VALUES (?, ?, ?)"
            " ON CONFLICT(date) DO UPDATE SET"
            "   gpt_input_tokens = gpt_input_tokens + ?,"
            "   gpt_output_tokens = gpt_output_tokens + ?",
            (today, input_tokens, output_tokens, input_tokens, output_tokens),
        )
        conn.commit()
        conn.close()
        log.info("Recorded GPT tokens: in=%d out=%d", input_tokens, output_tokens)
    except Exception as e:
        log.error("Failed to record gpt tokens: %s", e)


def _get_usage_since(since_date: str) -> Dict[str, float]:
    """Return {whisper_seconds, gpt_input_tokens, gpt_output_tokens, cost_usd}."""
    try:
        conn = _ensure_db()
        cursor = conn.execute(
            "SELECT COALESCE(SUM(whisper_seconds), 0),"
            "       COALESCE(SUM(gpt_input_tokens), 0),"
            "       COALESCE(SUM(gpt_output_tokens), 0)"
            " FROM usage WHERE date >= ?",
            (since_date,),
        )
        ws, gin, gout = cursor.fetchone()
        conn.close()
        cost = (
            ws * PRICE_WHISPER_PER_SEC
            + gin * PRICE_GPT4O_MINI_INPUT_PER_TOKEN
            + gout * PRICE_GPT4O_MINI_OUTPUT_PER_TOKEN
        )
        return {
            "whisper_seconds": float(ws),
            "gpt_input_tokens": int(gin),
            "gpt_output_tokens": int(gout),
            "cost_usd": float(cost),
        }
    except Exception as e:
        log.error("Failed to read usage: %s", e)
        return {"whisper_seconds": 0.0, "gpt_input_tokens": 0, "gpt_output_tokens": 0, "cost_usd": 0.0}


def get_usage_today() -> Dict[str, float]:
    return _get_usage_since(datetime.now().strftime("%Y-%m-%d"))


def get_usage_week() -> Dict[str, float]:
    return _get_usage_since((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))


def get_usage_month() -> Dict[str, float]:
    return _get_usage_since((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))


def get_usage_all() -> Dict[str, float]:
    return _get_usage_since("1970-01-01")
