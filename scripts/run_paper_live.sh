#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/Users/chiaclaw/Projects/hip3-scanner"
cd "$REPO_DIR"
mkdir -p output
mkdir -p output/logs

export HIP3_SCAN_INTERVAL_SECONDS="${HIP3_SCAN_INTERVAL_SECONDS:-10}"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] starting hip3 paper loop (interval=${HIP3_SCAN_INTERVAL_SECONDS}s, state=./output/paper_trader_state_live_1000.json)"
exec ./.venv/bin/python -m hip3_scanner.cli loop --paper --paper-state ./output/paper_trader_state_live_1000.json
