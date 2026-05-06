#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/Users/chiaclaw/Projects/hip3-scanner"
cd "$REPO_DIR"
mkdir -p output
mkdir -p output/logs
LOG_FILE="./output/logs/paper_live_watchdog.log"

if pgrep -af '/Users/chiaclaw/Projects/hip3-scanner/scripts/watch_paper_live.sh|hip3_scanner.cli loop --paper --paper-state ./output/paper_trader_state_live_1000.json' >/dev/null; then
  echo "already running"
  exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] fallback starting watchdog" | tee -a "$LOG_FILE"
nohup ./scripts/watch_paper_live.sh >> "$LOG_FILE" 2>&1 &
echo "started watchdog pid=$!"
