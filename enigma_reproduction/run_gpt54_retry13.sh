#!/usr/bin/env bash
# Re-run only the 13 gpt-5.4 tasks that previously aborted with
# `enterprise_insufficient_balance` (TeamoRouter wallet ran out mid-run).
set -euo pipefail
ENIGMA_TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ENIGMA_TASK_ROOT/.venv-enigma/bin/python" -u \
  "$ENIGMA_TASK_ROOT/enigma_reproduction/run_nyu.py" \
  --settings "$ENIGMA_TASK_ROOT/enigma_reproduction/gpt54_settings.json" \
  --run --num-workers 3 --per-instance 1.0 --total 13 \
  --ids \
    2020q-cry-adversarial \
    2018q-msc-algebra \
    2020q-rev-baby_mult \
    2018q-pwn-bigboy \
    2020q-rev-ezbreezy \
    2018q-pwn-get_it \
    2020q-rev-not_malware \
    2022f-cry-open_ellipti_ph \
    2022f-cry-polly_crack_this \
    2022f-rev-roulette \
    2020q-pwn-slithery \
    2020q-cry-smallsurp \
    2020f-cry-the_matrix \
    "$@"
