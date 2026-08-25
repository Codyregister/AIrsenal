#!/bin/bash
# Weekly automated transfer suggestion run for AIrsenal - Team 2 (3776647),
# running the "auto" chip-timing strategy at risk_lambda=0.5 (our best
# candidate from replay testing). This is the other arm of a live A/B test
# against Team 1 (3178353, greedy) - see TODO.md for the replay evidence
# that motivated the comparison. Team 2's DB was seeded 2026-08-12 with an
# exact copy of Team 1's initial squad (not an independent GA build), so
# the comparison isolates chip-timing strategy as the only variable.
#
# Suggestion-only: NEVER pass --apply_transfers or --clean here. Surfaced
# on the dashboard (tools/dashboard_app.py) - nothing here touches the
# live FPL team.
set -euo pipefail

FPL_TEAM_ID_TEAM2=3776647

cd /root/airsenal_replay
# hardcoded PATH, not `source ~/.local/bin/env` - cron's environment
# doesn't reliably set HOME, and that env script itself references $HOME
# unguarded. Silently killed price_change_snapshot_run.sh under cron for
# over a day (set -u -> "HOME: unbound variable") - see its comment.
export PATH="/root/.local/bin:$PATH"

export FPL_TEAM_ID="$FPL_TEAM_ID_TEAM2"
export AIRSENAL_HOME=/root/airsenal_home_dashboard_team2

LOG_DIR=/root/airsenal_replay/weekly_run_logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S)_team2.log"

{
  echo "=== Weekly transfer run (team2_auto) started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  uv run airsenal_run_pipeline \
    --weeks_ahead 3 \
    --fpl_team_id "$FPL_TEAM_ID_TEAM2" \
    --chip_strategy auto \
    --risk_lambda 0.5
  echo "=== Weekly transfer run (team2_auto) finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG_FILE" 2>&1

# keep the last 12 weeks of logs, prune older ones
find "$LOG_DIR" -name "*.log" -mtime +84 -delete
