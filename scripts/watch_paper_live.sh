#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="/Users/chiaclaw/Projects/hip3-scanner"
cd "$REPO_DIR"
mkdir -p output
mkdir -p output/logs
LOG_FILE="./output/logs/paper_live_watchdog.log"

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] watchdog launch" | tee -a "$LOG_FILE"
  set +e
  ./scripts/run_paper_live.sh >> "$LOG_FILE" 2>&1
  exit_code=$?
  set -e
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] scanner exited with code ${exit_code}; restarting in 5s" | tee -a "$LOG_FILE"
  sleep 5
done
