#!/bin/bash
# Runs the price-change snapshot fetch (see
# airsenal/scripts/fill_price_change_snapshot.py) against the live dashboard
# DB. Read-only against the FPL API - just records a data point, never
# applies anything to the live team. Meant to run several times a day (cron
# below) so momentum data stays reasonably fresh without hammering the API.
set -euo pipefail

cd /root/airsenal_replay
# hardcoded PATH, not `source ~/.local/bin/env` - cron's environment
# doesn't reliably set HOME, and that env script itself references $HOME
# unguarded. Silently killed this exact script under cron (set -u ->
# "HOME: unbound variable", before the log file even gets created) for
# over a day.
export PATH="/root/.local/bin:$PATH"

export FPL_TEAM_ID=3178353
export AIRSENAL_HOME=/root/airsenal_home_dashboard

LOG_DIR=/root/airsenal_replay/price_change_logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"

uv run python3 -m airsenal.scripts.fill_price_change_snapshot >> "$LOG_FILE" 2>&1

# keep the last 2 weeks of logs
find "$LOG_DIR" -name "*.log" -mtime +14 -delete
