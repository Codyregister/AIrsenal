#!/bin/bash
# Weekly automated transfer suggestion run for AIrsenal - Team 1 (3178353),
# running the "greedy" chip strategy (play a chip whenever the tree search
# finds it optimal within the horizon, matching AIrsenal's original
# pre-chip-timing default). This is one arm of a live A/B test against
# Team 2 (auto, risk_lambda=0.5) - see TODO.md for the replay evidence that
# motivated the comparison (greedy currently leads auto 2 seasons to 0,
# but auto came within 0.3% once - too close to call from replay alone).
# Suggestion-only: NEVER pass --apply_transfers or --clean here. Surfaced
# on the dashboard (tools/dashboard_app.py) - nothing here touches the
# live FPL team.
set -euo pipefail

cd /root/airsenal_replay
# hardcoded PATH, not `source ~/.local/bin/env` - cron's environment
# doesn't reliably set HOME, and that env script itself references $HOME
# unguarded. Silently killed price_change_snapshot_run.sh under cron for
# over a day (set -u -> "HOME: unbound variable") - see its comment.
export PATH="/root/.local/bin:$PATH"

export FPL_TEAM_ID=3178353
export AIRSENAL_HOME=/root/airsenal_home_dashboard

LOG_DIR=/root/airsenal_replay/weekly_run_logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"

{
  echo "=== Weekly transfer run (team1_greedy) started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  uv run airsenal_run_pipeline \
    --weeks_ahead 3 \
    --fpl_team_id 3178353 \
    --chip_strategy manual \
    --wildcard_week 0 \
    --free_hit_week 0 \
    --triple_captain_week 0 \
    --bench_boost_week 0
  echo "=== Weekly transfer run (team1_greedy) finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG_FILE" 2>&1

# keep the last 12 weeks of logs, prune older ones
find "$LOG_DIR" -name "*.log" -mtime +84 -delete
