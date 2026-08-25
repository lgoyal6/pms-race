#!/bin/sh
# Full reproduce: starts the mock PMS, runs both stages, writes RESULTS.md.
# Stdlib python3 only. No network, no dependencies. About 9 minutes.
# TRIALS / ATRIALS can be lowered for a fast smoke run.
set -e
cd "$(dirname "$0")"

TRIALS=${TRIALS:-6}
ATRIALS=${ATRIALS:-15}

python3 server.py 8799 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
sleep 1

python3 harness.py --stage sweep     --trials "$TRIALS"          --out results.sweep.json
python3 harness.py --stage anomalies --anomaly-trials "$ATRIALS" --out results.anomalies.json
python3 report.py RESULTS.md

echo "done: RESULTS.md and report.html"
