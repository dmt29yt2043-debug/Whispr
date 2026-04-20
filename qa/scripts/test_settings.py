"""TC_016, TC_017, TC_018, TC_028 — settings persistence, recovery, concurrency."""
import os
import json
import shutil
import threading
import time
from pathlib import Path
from _harness import case, run_all

import settings as S

# Point settings at a temp location so we don't clobber the real one
_TMP_DIR = Path("/tmp/qa_whisper_settings")
_TMP_FILE = _TMP_DIR / "settings.json"

# Monkey-patch settings module paths
S._CONFIG_DIR = str(_TMP_DIR)
S._SETTINGS_FILE = str(_TMP_FILE)


def _reset():
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    # Reset cache
    S._cache = None


@case("TC_017", "settings", "missing file → defaults used, no file created until set()")
def test_missing_file():
    _reset()
    # File doesn't exist
    assert not _TMP_FILE.exists()
    assert S.get("mode") == S.MODE_AUTO
    # Still not created (load() caches defaults in memory but doesn't write)
    assert not _TMP_FILE.exists()


@case("TC_016", "settings", "corrupted JSON → defaults, no crash")
def test_corrupted_file():
    _reset()
    _TMP_FILE.write_text("this is not valid json { { {")
    S._cache = None  # force reload
    assert S.get("mode") == S.MODE_AUTO  # defaults
    # After a .set(), file should be valid JSON again
    S.set("mode", S.MODE_LOCAL)
    data = json.loads(_TMP_FILE.read_text())
    assert data["mode"] == S.MODE_LOCAL


@case("TC_SET_PERSIST", "settings", "set() persists across reload()")
def test_persist():
    _reset()
    S.set("hotkey", "right_shift")
    S.reload()
    assert S.get("hotkey") == "right_shift"


@case("TC_SET_DEEPCOPY", "settings", "get() returns deep copy of dict values")
def test_deep_copy():
    _reset()
    S.set("app_tones", {"com.foo": "casual"})
    tones = S.get("app_tones")
    tones["com.bar"] = "professional"  # mutate return value
    # Internal cache must NOT be affected
    tones2 = S.get("app_tones")
    assert "com.bar" not in tones2, "get() returned shared reference — mutation leaked"


@case("TC_028", "settings", "50 concurrent set() from 20 threads — no lost writes")
def test_concurrent_writes():
    _reset()
    NUM_KEYS = 50

    def worker(i):
        S.set(f"test_key_{i}", i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_KEYS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Reload from disk
    S.reload()
    missing = []
    for i in range(NUM_KEYS):
        if S.get(f"test_key_{i}") != i:
            missing.append(i)
    assert not missing, f"Lost writes for keys: {missing[:10]}…"


@case("TC_ATOMIC", "settings", "settings file never in 0-byte state under concurrent writes")
def test_atomic_write():
    _reset()
    stop = [False]
    errors = []

    def writer():
        while not stop[0]:
            try:
                S.set("x", os.urandom(8).hex())
            except Exception as e:
                errors.append(e)

    def reader():
        while not stop[0]:
            try:
                if _TMP_FILE.exists() and _TMP_FILE.stat().st_size == 0:
                    errors.append("0-byte settings.json observed")
                    return
            except Exception:
                pass

    wt = [threading.Thread(target=writer) for _ in range(5)]
    rt = [threading.Thread(target=reader) for _ in range(3)]
    for t in wt + rt: t.start()
    time.sleep(0.5)
    stop[0] = True
    for t in wt + rt: t.join()

    assert not errors, f"Observed {errors[:3]}"


if __name__ == "__main__":
    run_all("test_settings")
