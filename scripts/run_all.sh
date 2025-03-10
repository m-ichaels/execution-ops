#!/usr/bin/env bash
# Full pipeline: data -> store -> calendar, corporate-action and fixed-income checks -> the day pipeline -> tests -> figures
# -> summary -> report.   Usage: scripts/run_all.sh [--skip-download] [--quick]
set -euo pipefail
FROM=2026-07-01; TO=2026-09-16
[[ " $* " == *" --quick "* ]] && { FROM=2026-09-08; TO=2026-09-11; }
[[ " $* " == *" --skip-download "* ]] || python tools/download.py
python -m xops build-store
python -m xops calendar-check
python -m xops corpact-check
python -m xops fi-check
python -m xops run --from $FROM --to $TO --faults alternate --seed 1 | tee results/run.log
python -m pytest -q | tee results/tests.txt
python -m xops selftest --port 9881 | tee -a results/tests.txt
python scripts/plots.py
python scripts/summarize.py
python scripts/report.py
echo done
