#!/bin/bash
# Deadline-aware auto-apply for Team 2 (3776647, chip_strategy=auto,
# risk_lambda=0.5). Run HOURLY from cron; it decides for itself whether this
# is the hour to act.
#
# Why hourly rather than a fixed weekly slot: FPL deadlines move (GW3 was
# Friday 17:30 UTC, GW4 is Saturday 12:30 UTC), and applying transfers wants
# the freshest injury/price data, so a static Thursday slot would often be two
# days stale. This wakes up every hour, asks the API when the next deadline
# is, and only does real work inside the window below.
#
# It fires ONCE per gameweek: a state file records the last gameweek applied,
# so a missed hour (reboot, etc.) is caught up on the next run as long as the
# window has not closed.
#
# This is the ONLY automation permitted to write to a live FPL team, and only
# Team 2. Team 1 (3178353) remains suggestion-only via
# weekly_transfer_run_team1_greedy.sh. Chip weeks are always skipped and
# alerted rather than applied - see tools/apply_transfers_if_safe.py.
set -uo pipefail

# hardcoded PATH, not `source ~/.local/bin/env` - cron's environment doesn't
# reliably set HOME, and that env script references $HOME unguarded. Silently
# killed price_change_snapshot_run.sh under cron for over a day.
export PATH="/root/.local/bin:$PATH"
export JAX_PLATFORMS=cpu

FPL_TEAM_ID_TEAM2=3776647
REPO=/root/airsenal_replay
export FPL_TEAM_ID="$FPL_TEAM_ID_TEAM2"
export AIRSENAL_HOME=/root/airsenal_home_dashboard_team2

# Act when the deadline is this close (hours). The pipeline refresh takes
# ~10 min, so 3h leaves ample margin while still being late enough to pick up
# Friday team news.
WINDOW_START_H=3.5
WINDOW_END_H=0.5

STATE_FILE=/root/airsenal_replay/.last_auto_applied_gw_team2
LOG_DIR=$REPO/apply_run_logs
mkdir -p "$LOG_DIR"

cd "$REPO" || exit 1

# Ask the API which gameweek is next and how long until its deadline.
read -r NEXT_GW HOURS_LEFT < <(uv run python - <<'PY'
from datetime import datetime, timezone
from curl_cffi import requests
try:
    s = requests.Session(impersonate="chrome")
    b = s.get("https://fantasy.premierleague.com/api/bootstrap-static/").json()
    now = datetime.now(timezone.utc)
    for e in b["events"]:
        dl = datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))
        if dl > now:
            print(e["id"], (dl - now).total_seconds() / 3600.0)
            break
    else:
        print(-1, -1)
except Exception:
    print(-1, -1)
PY
)

if [ "${NEXT_GW:--1}" = "-1" ]; then
  # Never fail loudly on a transient API hiccup - there will be another hour.
  exit 0
fi

in_window=$(awk -v h="$HOURS_LEFT" -v a="$WINDOW_START_H" -v b="$WINDOW_END_H" \
  'BEGIN { print (h <= a && h >= b) ? 1 : 0 }')
[ "$in_window" = "1" ] || exit 0

# Once per gameweek.
LAST_GW=$(cat "$STATE_FILE" 2>/dev/null || echo "")
[ "$LAST_GW" = "$NEXT_GW" ] && exit 0

LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S)_team2_gw${NEXT_GW}.log"
{
  echo "=== auto-apply run (team2) GW${NEXT_GW}, ${HOURS_LEFT}h to deadline, started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

  # Refresh predictions and suggestions first. Deliberately WITHOUT
  # --apply_transfers: applying is done by apply_transfers_if_safe.py, which
  # enforces the chip guard the pipeline has no notion of.
  uv run airsenal_run_pipeline \
    --weeks_ahead 3 \
    --fpl_team_id "$FPL_TEAM_ID_TEAM2" \
    --chip_strategy auto \
    --risk_lambda 0.5 </dev/null
  pipeline_rc=$?
  echo "--- pipeline exit=${pipeline_rc}"

  if [ "$pipeline_rc" -ne 0 ]; then
    echo "!!! pipeline failed - not applying anything"
    exit 1
  fi

  uv run python tools/apply_transfers_if_safe.py \
    --fpl_team_id "$FPL_TEAM_ID_TEAM2" </dev/null
  apply_rc=$?
  echo "--- apply exit=${apply_rc} (0=applied/nothing to do, 2=skipped for a human)"

  # Record the gameweek for any outcome that shouldn't be retried this week.
  # An outright error (1) is left unrecorded so the next hour tries again.
  if [ "$apply_rc" -eq 0 ] || [ "$apply_rc" -eq 2 ]; then
    echo "$NEXT_GW" > "$STATE_FILE"
  fi

  echo "=== finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG_FILE" 2>&1

# keep 12 weeks of logs
find "$LOG_DIR" -name "*.log" -mtime +84 -delete
