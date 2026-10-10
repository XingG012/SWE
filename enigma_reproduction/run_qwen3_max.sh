#!/usr/bin/env bash
set -euo pipefail
ENIGMA_TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ENIGMA_TASK_ROOT/.venv-enigma/bin/python" -u \
  "$ENIGMA_TASK_ROOT/enigma_reproduction/run_nyu.py" \
  --settings "$ENIGMA_TASK_ROOT/enigma_reproduction/qwen3_max_settings.json" \
  --run --num-workers 3 --per-instance 0.1 --total 8.2 "$@"
