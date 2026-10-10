#!/usr/bin/env bash
set -euo pipefail
ENIGMA_TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ENIGMA_TASK_ROOT"
exec bash enigma_reproduction/run_qwen3_max.sh \
  --num-workers 3 --total 0.3 \
  --ids 2021q-rev-checker 2019q-rev-beleaf 2022q-rev-the_big_bang "$@"
