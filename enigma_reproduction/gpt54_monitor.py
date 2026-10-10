"""Watch the GPT-5.4 full run (parent PID given on argv) until it exits.

Appends one health line per interval to generated/gpt54-monitor.log and prints a
final summary to stdout when the run finishes. Hard-failure signals are counted
with patterns that avoid false positives (e.g. the SHA256 / float digit '429').
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARALLEL = ROOT / "enigma_reproduction/generated/parallel-20261009T155550158174Z"
RESULT_DIRS = ["run-20261009T155553757531Z", "run-20261009T155553756344Z",
               "run-20261009T155553764542Z"]
MONITOR = ROOT / "enigma_reproduction/generated/gpt54-monitor.log"
INTERVAL = 180
HARD_ERRORS = ["API request failed", "BadRequestError",
               "not supported by any configured", "request blocked",
               "no valid usage", "infrastructure_error"]


def alive(pid):
    return Path(f"/proc/{pid}").exists()


def completed():
    total = 0
    for name in RESULT_DIRS:
        path = ROOT / "enigma_reproduction/generated" / name / "results.json"
        if path.is_file():
            try:
                total += len(json.loads(path.read_text()))
            except (OSError, ValueError):
                pass
    return total


def hard_errors():
    count = 0
    for log in PARALLEL.glob("worker-*.log"):
        try:
            text = log.read_text(errors="ignore")
        except OSError:
            continue
        for needle in HARD_ERRORS:
            count += text.count(needle)
    return count


def main():
    parent = int(sys.argv[1])
    with MONITOR.open("a") as out:
        while alive(parent):
            line = (f"{datetime.now().isoformat(timespec='seconds')} "
                    f"completed={completed()} hard_errors={hard_errors()}")
            out.write(line + "\n")
            out.flush()
            time.sleep(INTERVAL)
        out.write(f"{datetime.now().isoformat(timespec='seconds')} RUN_FINISHED\n")
    print(json.dumps({"finished": True, "completed": completed(),
                      "hard_errors": hard_errors(),
                      "monitor_log": str(MONITOR)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
