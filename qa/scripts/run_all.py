"""Run every test_*.py script and aggregate results.

Usage:  python3 run_all.py
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

SCRIPTS = sorted(p for p in HERE.glob("test_*.py"))


def main():
    overall = {"scripts": {}, "summary": {"pass": 0, "fail": 0, "error": 0, "total": 0}}

    for script in SCRIPTS:
        name = script.stem
        print(f"\n### Running {name} ###")
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(HERE),
            capture_output=True,
            text=True,
        )
        print(proc.stdout)
        if proc.returncode != 0:
            print(f"STDERR:\n{proc.stderr}")

        result_file = RESULTS_DIR / f"{name}.json"
        if result_file.exists():
            data = json.loads(result_file.read_text())
            overall["scripts"][name] = data["results"]
            for r in data["results"]:
                overall["summary"]["total"] += 1
                overall["summary"][r["status"].lower()] = overall["summary"].get(r["status"].lower(), 0) + 1

    # Aggregate
    agg_file = RESULTS_DIR / "test_results.json"
    agg_file.write_text(json.dumps(overall, indent=2))

    s = overall["summary"]
    print("\n" + "=" * 60)
    print(f"OVERALL: {s.get('pass', 0)}/{s['total']} PASS, {s.get('fail', 0)} FAIL, {s.get('error', 0)} ERROR")
    print(f"Results written to: {agg_file}")


if __name__ == "__main__":
    main()
