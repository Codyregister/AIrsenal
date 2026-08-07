#!/bin/bash
# Weekly automated transfer suggestion run for AIrsenal.
# Updates the DB, generates fresh predictions, and computes a transfer +
# chip suggestion (chip_strategy=auto, risk_lambda=0.5 - our validated best
# candidate from replay testing, see TODO.md/memory for methodology and
# caveats). Suggestion-only: NEVER pass --apply_transfers or --clean here.
# Surfaced on the dashboard (tools/dashboard_app.py) - nothing here touches
# the live FPL team.
set -euo pipefail

cd /root/airsenal_replay
source "$HOME/.local/bin/env"

export FPL_TEAM_ID=742663
export AIRSENAL_HOME=/root/airsenal_home_dashboard

LOG_DIR=/root/airsenal_replay/weekly_run_logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"

{
  echo "=== Weekly transfer run started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  uv run airsenal_run_pipeline \
    --weeks_ahead 3 \
    --fpl_team_id 742663 \
    --chip_strategy auto \
    --risk_lambda 0.5
  echo "=== Weekly transfer run finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG_FILE" 2>&1

# keep the last 12 weeks of logs, prune older ones
find "$LOG_DIR" -name "*.log" -mtime +84 -delete
