#!/usr/bin/env bash
# Run the full Route A evidence suite under one shared run id so every output
# lands in data/experiments/<run-id>/ and paper_tables.py can collect them all.
#
#   ./run_all.sh                # mock LLM (offline, deterministic), full suite
#   ./run_all.sh --live         # use the configured LLM endpoint
#   REPEAT=3 TRIALS=20 ./run_all.sh
#
# Robot side effects are stubbed inside the Python harnesses; nothing here
# moves the robot or requires a speaker/microphone.
set -euo pipefail

cd "$(dirname "$0")/.."

PY=.venv/bin/python
export XLEROBOT_RUN_ID="${XLEROBOT_RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
REPEAT="${REPEAT:-3}"
TRIALS="${TRIALS:-20}"

echo "== run id: ${XLEROBOT_RUN_ID} =="

if [ "${1:-}" = "--live" ]; then
  "$PY" experiments/run_llm_eval.py --live --repeat "$REPEAT"
else
  "$PY" experiments/run_llm_eval.py --repeat "$REPEAT"
fi
"$PY" experiments/run_latency_bench.py --trials "$TRIALS"
"$PY" experiments/run_reliability_report.py
"$PY" experiments/run_gait_pipeline_check.py
"$PY" experiments/paper_tables.py

echo
echo "All outputs: data/experiments/${XLEROBOT_RUN_ID}/"
echo "Tables:      papers/generated_tables.md"
