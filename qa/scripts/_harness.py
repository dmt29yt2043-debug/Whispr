"""Shared test harness — each test_* script imports this.

Usage:
    from _harness import case, run_all, app_path
    @case("TC_019", "anti_hallucination", "stripping noise tokens")
    def test_noise():
        assert filter_transcription("[BLANK_AUDIO]") == ""

run_all() walks every @case-decorated callable and records PASS/FAIL/ERROR
into results/<script_name>.json.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

_QA = Path(__file__).resolve().parent.parent
_APP = _QA.parent / "whisper-dictation"

# Make app modules importable
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

app_path = str(_APP)
results_dir = _QA / "results"
results_dir.mkdir(exist_ok=True)

_CASES = []  # list of (tc_id, area, desc, fn)


def case(tc_id: str, area: str, desc: str = ""):
    def _wrap(fn):
        _CASES.append((tc_id, area, desc, fn))
        return fn
    return _wrap


def _run_one(tc_id, area, desc, fn):
    t0 = time.time()
    try:
        fn()
        status = "PASS"
        err = None
    except AssertionError as e:
        status = "FAIL"
        err = f"AssertionError: {e}"
    except Exception as e:
        status = "ERROR"
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    dt_ms = int((time.time() - t0) * 1000)
    return {
        "id": tc_id,
        "area": area,
        "desc": desc,
        "status": status,
        "ms": dt_ms,
        "error": err,
    }


def run_all(script_name: str):
    print(f"\n=== {script_name} ===")
    results = []
    for tc_id, area, desc, fn in _CASES:
        r = _run_one(tc_id, area, desc, fn)
        symbol = {"PASS": "✓", "FAIL": "✗", "ERROR": "!"}[r["status"]]
        print(f"  {symbol} {r['id']:8s} [{r['area']:18s}] {r['desc']}  ({r['ms']}ms)")
        if r["status"] != "PASS":
            # Print error compactly
            err_line = (r["error"] or "").split("\n")[0]
            print(f"        {err_line}")
        results.append(r)

    # Write per-script result
    out = results_dir / f"{script_name}.json"
    out.write_text(json.dumps({"script": script_name, "results": results}, indent=2))

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    errored = sum(1 for r in results if r["status"] == "ERROR")
    total = len(results)
    print(f"  ---")
    print(f"  {passed}/{total} pass, {failed} fail, {errored} error")
    return results
