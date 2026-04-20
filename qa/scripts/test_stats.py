"""TC_027, TC_042, TC_044 — stats accuracy, concurrency, injection safety."""
import os
import shutil
import threading
from pathlib import Path
from _harness import case, run_all

import stats

# Redirect DB to tmp
_TMP_DIR = Path("/tmp/qa_whisper_stats")
stats._CONFIG_DIR = str(_TMP_DIR)
stats._DB_PATH = str(_TMP_DIR / "stats.db")


def _reset():
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    _TMP_DIR.mkdir(parents=True, exist_ok=True)


@case("TC_STATS_RECORD", "stats", "record_transcribe increments by_model correctly")
def test_record():
    _reset()
    stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, 10.0)
    stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, 5.0)
    stats.record_transcribe(stats.MODEL_LOCAL, 20.0)

    u = stats.get_usage_today()
    models = {m["model"]: m for m in u["by_model"]}
    assert stats.MODEL_GPT4O_MINI_TRANSCRIBE in models
    assert models[stats.MODEL_GPT4O_MINI_TRANSCRIBE]["seconds"] == 15.0
    assert models[stats.MODEL_GPT4O_MINI_TRANSCRIBE]["calls"] == 2
    assert models[stats.MODEL_LOCAL]["seconds"] == 20.0
    assert models[stats.MODEL_LOCAL]["cost_usd"] == 0.0


@case("TC_044", "stats", "paid_seconds excludes local, local_seconds separated")
def test_paid_vs_local():
    _reset()
    stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, 60.0)  # paid
    stats.record_transcribe(stats.MODEL_LOCAL, 120.0)                  # free

    u = stats.get_usage_today()
    assert u["paid_seconds"] == 60.0
    assert u["local_seconds"] == 120.0
    # Paid cost = 60s * 0.003/60 = 0.003
    assert abs(u["total_cost_usd"] - 0.003) < 1e-6


@case("TC_STATS_TOKENS", "stats", "GPT token cost computed correctly")
def test_gpt_tokens():
    _reset()
    stats.record_gpt_tokens(input_tokens=1_000_000, output_tokens=500_000)
    u = stats.get_usage_today()
    # $0.15/M in + $0.60/M out × 0.5 = 0.15 + 0.30 = 0.45
    assert abs(u["gpt_cost_usd"] - 0.45) < 1e-6


@case("TC_027", "stats", "10 threads × 100 increments — no lost writes")
def test_concurrent_writes():
    _reset()

    def worker():
        for _ in range(100):
            stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, 1.0)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    u = stats.get_usage_today()
    m = next(x for x in u["by_model"] if x["model"] == stats.MODEL_GPT4O_MINI_TRANSCRIBE)
    assert m["seconds"] == 1000.0, f"Expected 1000s, got {m['seconds']}"
    assert m["calls"] == 1000


@case("TC_042", "stats", "SQL-injection-like model name handled via parameterized query")
def test_sql_injection_safe():
    _reset()
    malicious = "'; DROP TABLE usage; --"
    stats.record_transcribe(malicious, 5.0)
    # DB must still be intact
    u = stats.get_usage_today()
    assert any(m["model"] == malicious for m in u["by_model"])
    # usage table still exists
    stats.record_gpt_tokens(10, 20)
    u = stats.get_usage_today()
    assert u["gpt_input_tokens"] == 10


@case("TC_STATS_WORD_COUNT", "stats", "word count increments across dictations")
def test_word_count():
    _reset()
    stats.record_words("one two three")
    stats.record_words("four five")
    assert stats.get_words_today() == 5


@case("TC_STATS_EMPTY_NO_RECORD", "stats", "empty text/zero seconds doesn't add rows")
def test_empty_no_record():
    _reset()
    stats.record_words("")
    stats.record_words("   ")
    stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, 0)
    stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, -5)
    assert stats.get_words_today() == 0
    u = stats.get_usage_today()
    assert len(u["by_model"]) == 0


if __name__ == "__main__":
    run_all("test_stats")
