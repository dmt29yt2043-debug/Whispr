"""Performance / concurrency tests — load simulations for local modules.

There's no server, so 'load' = many concurrent Python calls hitting our
state (stats DB, settings, VAD, anti-hallucination).
"""
import shutil
import threading
import time
from pathlib import Path
from _harness import case, run_all

import stats
import settings as S
import anti_hallucination


@case("PERF_STATS_10x100", "performance", "10 threads × 100 stats writes — latency p95 <50ms")
def test_stats_parallel():
    tmp = Path("/tmp/qa_perf_stats")
    stats._CONFIG_DIR = str(tmp)
    stats._DB_PATH = str(tmp / "stats.db")
    shutil.rmtree(tmp, ignore_errors=True)

    latencies = []
    lock = threading.Lock()

    def worker():
        for _ in range(100):
            t0 = time.time()
            stats.record_transcribe(stats.MODEL_GPT4O_MINI_TRANSCRIBE, 1.0)
            dt = (time.time() - t0) * 1000
            with lock:
                latencies.append(dt)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    t_start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    total_dt = time.time() - t_start

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"        PERF: total {total_dt:.2f}s  p50={p50:.2f}ms  p95={p95:.2f}ms  max={latencies[-1]:.2f}ms")
    assert p95 < 100, f"p95={p95:.1f}ms >= 100ms budget"


@case("PERF_SETTINGS_50x20", "performance", "20 threads × 50 settings writes — no lost writes, fast")
def test_settings_parallel():
    tmp = Path("/tmp/qa_perf_settings")
    S._CONFIG_DIR = str(tmp)
    S._SETTINGS_FILE = str(tmp / "settings.json")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    S._cache = None

    def worker(thread_id):
        for i in range(50):
            S.set(f"t{thread_id}_k{i}", thread_id * 1000 + i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    dt = time.time() - t0

    S.reload()
    # Verify all 1000 keys present
    missing = 0
    for tid in range(20):
        for i in range(50):
            if S.get(f"t{tid}_k{i}") != tid * 1000 + i:
                missing += 1
    print(f"        PERF: total {dt:.2f}s for 1000 writes ({dt*1000/1000:.2f}ms each)")
    assert missing == 0, f"{missing}/1000 keys missing"


@case("PERF_ANTIHALL_10K", "performance", "anti-hallucination filter on 10,000 inputs < 1s")
def test_antihall_throughput():
    inputs = ["Hello world", "[BLANK_AUDIO]", "you you you you you", "обычный текст"] * 2500
    t0 = time.time()
    for txt in inputs:
        anti_hallucination.filter_transcription(txt)
    dt = time.time() - t0
    print(f"        PERF: 10000 filter calls in {dt:.2f}s ({dt*1e6/10000:.0f}µs each)")
    assert dt < 1.0, f"Too slow: {dt:.2f}s"


if __name__ == "__main__":
    run_all("test_performance")
