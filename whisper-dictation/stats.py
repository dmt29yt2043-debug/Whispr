"""Statistics — word count + per-model transcription usage + GPT tokens + costs."""

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, List, Any

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.expanduser("~/.whisper-dictation")
_DB_PATH = os.path.join(_CONFIG_DIR, "stats.db")

# Canonical model names used in stats rows
MODEL_GPT4O_MINI_TRANSCRIBE = "gpt-4o-mini-transcribe"
MODEL_WHISPER_1 = "whisper-1"
MODEL_LOCAL = "local-faster-whisper"

# Price per second for each transcription model (USD).
# Keep in sync with OpenAI pricing.
TRANSCRIBE_PRICE_PER_SEC: Dict[str, float] = {
    MODEL_GPT4O_MINI_TRANSCRIBE: 0.003 / 60.0,   # $0.003/min
    MODEL_WHISPER_1:             0.006 / 60.0,   # $0.006/min
    MODEL_LOCAL:                 0.0,            # free
}

# GPT-4o-mini (cleanup): $0.15 per 1M input, $0.60 per 1M output
PRICE_GPT4O_MINI_INPUT_PER_TOKEN = 0.15 / 1_000_000
PRICE_GPT4O_MINI_OUTPUT_PER_TOKEN = 0.60 / 1_000_000

# Legacy constant — kept for backward compat with any external readers
PRICE_WHISPER_PER_SEC = TRANSCRIBE_PRICE_PER_SEC[MODEL_GPT4O_MINI_TRANSCRIBE]


@contextmanager
def _db():
    """Context manager: always closes the SQLite connection, even on error.

    BUG FIX #22: previous code called conn.close() only on the happy
    path, leaking the connection if an exception happened between
    _ensure_db() and close().
    """
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    try:
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
        # Per-model transcription stats (date + model composite key)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transcribe_usage ("
            "  date TEXT NOT NULL,"
            "  model TEXT NOT NULL,"
            "  seconds REAL NOT NULL DEFAULT 0,"
            "  calls INTEGER NOT NULL DEFAULT 0,"
            "  PRIMARY KEY (date, model)"
            ")"
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


# ── Words ───────────────────────────────────────────────────────────

def record_words(text: str) -> None:
    """Record word count for today."""
    if not text.strip():
        return
    word_count = len(text.split())
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO word_counts (date, count) VALUES (?, ?)"
                " ON CONFLICT(date) DO UPDATE SET count = count + ?",
                (today, word_count, word_count),
            )
            conn.commit()
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
        with _db() as conn:
            cursor = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM word_counts WHERE date >= ?",
                (since_date,),
            )
            return cursor.fetchone()[0]
    except Exception as e:
        log.error("Failed to get stats: %s", e)
        return 0


# ── Usage (seconds / tokens) ────────────────────────────────────────

def record_transcribe(model: str, seconds: float) -> None:
    """Record a transcription call with its model name + audio duration.

    Also mirrors to the legacy `usage.whisper_seconds` column so older
    queries keep working.
    """
    if seconds <= 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _db() as conn:
            # Per-model breakdown
            conn.execute(
                "INSERT INTO transcribe_usage (date, model, seconds, calls) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(date, model) DO UPDATE SET "
                "  seconds = seconds + ?, calls = calls + 1",
                (today, model, seconds, seconds),
            )
            # Legacy aggregate — only PAID API time (excludes free local)
            if model != MODEL_LOCAL:
                conn.execute(
                    "INSERT INTO usage (date, whisper_seconds) VALUES (?, ?)"
                    " ON CONFLICT(date) DO UPDATE SET whisper_seconds = whisper_seconds + ?",
                    (today, seconds, seconds),
                )
            conn.commit()
        log.info("Recorded %.2fs of %s usage", seconds, model)
    except Exception as e:
        log.error("Failed to record transcribe usage: %s", e)


def record_whisper_seconds(seconds: float) -> None:
    """Legacy: record audio without model info. Prefer record_transcribe()."""
    # Attribute legacy calls to the primary paid model.
    record_transcribe(MODEL_GPT4O_MINI_TRANSCRIBE, seconds)


def record_gpt_tokens(input_tokens: int, output_tokens: int) -> None:
    """Record GPT-4o-mini token usage."""
    if input_tokens <= 0 and output_tokens <= 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO usage (date, gpt_input_tokens, gpt_output_tokens) VALUES (?, ?, ?)"
                " ON CONFLICT(date) DO UPDATE SET"
                "   gpt_input_tokens = gpt_input_tokens + ?,"
                "   gpt_output_tokens = gpt_output_tokens + ?",
                (today, input_tokens, output_tokens, input_tokens, output_tokens),
            )
            conn.commit()
        log.info("Recorded GPT tokens: in=%d out=%d", input_tokens, output_tokens)
    except Exception as e:
        log.error("Failed to record gpt tokens: %s", e)


def _get_usage_since(since_date: str) -> Dict[str, Any]:
    """Return usage summary with per-model breakdown.

    Schema:
      by_model: [{model, seconds, calls, cost_usd}, ...]
      gpt_input_tokens, gpt_output_tokens, gpt_cost_usd
      whisper_seconds (legacy, paid time only)
      total_cost_usd
    """
    empty = {
        "by_model": [],
        "gpt_input_tokens": 0,
        "gpt_output_tokens": 0,
        "gpt_cost_usd": 0.0,
        "whisper_seconds": 0.0,
        "paid_seconds": 0.0,
        "local_seconds": 0.0,
        "cost_usd": 0.0,        # legacy alias
        "total_cost_usd": 0.0,
    }
    try:
        with _db() as conn:
            # Per-model transcription breakdown
            cur = conn.execute(
                "SELECT model, COALESCE(SUM(seconds), 0), COALESCE(SUM(calls), 0) "
                "FROM transcribe_usage WHERE date >= ? GROUP BY model",
                (since_date,),
            )
            rows = cur.fetchall()
            # GPT cleanup tokens
            cur2 = conn.execute(
                "SELECT COALESCE(SUM(gpt_input_tokens), 0),"
                "       COALESCE(SUM(gpt_output_tokens), 0)"
                " FROM usage WHERE date >= ?",
                (since_date,),
            )
            gin, gout = cur2.fetchone()

        by_model = []
        paid_seconds = 0.0
        local_seconds = 0.0
        transcribe_cost = 0.0
        for model, seconds, calls in rows:
            price = TRANSCRIBE_PRICE_PER_SEC.get(model, 0.0)
            model_cost = seconds * price
            by_model.append({
                "model": model,
                "seconds": float(seconds),
                "calls": int(calls),
                "cost_usd": float(model_cost),
            })
            transcribe_cost += model_cost
            if model == MODEL_LOCAL:
                local_seconds += seconds
            else:
                paid_seconds += seconds

        gpt_cost = (
            gin * PRICE_GPT4O_MINI_INPUT_PER_TOKEN
            + gout * PRICE_GPT4O_MINI_OUTPUT_PER_TOKEN
        )
        total = transcribe_cost + gpt_cost

        return {
            "by_model": sorted(by_model, key=lambda x: -x["seconds"]),
            "gpt_input_tokens": int(gin),
            "gpt_output_tokens": int(gout),
            "gpt_cost_usd": float(gpt_cost),
            "whisper_seconds": float(paid_seconds),  # legacy: paid only
            "paid_seconds": float(paid_seconds),
            "local_seconds": float(local_seconds),
            "cost_usd": float(total),                # legacy alias
            "total_cost_usd": float(total),
        }
    except Exception as e:
        log.error("Failed to read usage: %s", e)
        return empty


def get_usage_today() -> Dict[str, float]:
    return _get_usage_since(datetime.now().strftime("%Y-%m-%d"))


def get_usage_week() -> Dict[str, float]:
    return _get_usage_since((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))


def get_usage_month() -> Dict[str, float]:
    return _get_usage_since((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))


def get_usage_all() -> Dict[str, float]:
    return _get_usage_since("1970-01-01")
