# Dashboard: potential improvements

Notes-to-self from building `dashboard_app.py` (2026-08-05). Fork-only —
these are ideas for our own use, not upstream AIrsenal feature requests.
Roughly ordered by how much value they'd add for the effort involved.

## Reliability / operability

- **Run as a systemd service, not a bare tmux window.** Currently it's
  killed if the tmux session dies or the host reboots, and nothing restarts
  it automatically. A simple unit file with `Restart=on-failure` would fix
  both. Low effort, meaningful durability win.
- **Front it with gunicorn (or similar) instead of Flask's dev server.**
  We're already seeing "WARNING: this is a development server" in the logs.
  Fine for a LAN-only tool, but worth doing before trusting it with anything
  more.
- **Cache expensive computations.** `build_chip_report` and the squad/
  predictions panel recompute everything from scratch on every page load
  (including re-fitting model queries). A short TTL cache (even just
  in-memory, keyed on the latest prediction tag) would make repeat loads
  much faster and reduce DB load if multiple people/tabs hit it.
- **Health/error visibility for the replay jobs themselves.** The "Replay
  tmux session" stat just counts windows — it doesn't distinguish "still
  running happily" from "crashed and left a dead window." Worth parsing the
  `.log` files for recent `ERROR`/`Traceback` lines and surfacing that.

## Cross-host aggregation

- **Auto-fetch replay results from other hosts (tower).** Right now the
  "Replay validation results" panel only reads local files
  (`/root/airsenal_replay/results/`) — anything computed on tower has to be
  manually rsynced over to show up. A small periodic sync job (cron + rsync,
  or the dashboard doing an SSH pull on a timer) would remove that manual
  step. Given we're now routing heavy work through tower specifically, this
  is worth doing soon rather than continuing to sync by hand.
- **Multi-host ops status.** Since compute is now split across apollol/tower,
  it'd be useful to show both hosts' status (load, memory, what's running)
  in one place rather than SSHing into each separately.

## Content

- **Historical squad/transfer tracking.** Currently only shows the *current*
  squad. A simple "transfers made this season" timeline (pulled from the
  `Transaction` table) would make it easy to see what AIrsenal has actually
  done over a season, not just its current state.
- **Live gameweek results once the season is underway.** Right now the
  dashboard is prediction-only; once real gameweeks start, showing actual
  points scored (per player and total) alongside what was predicted would
  make the accuracy of the model visible over time, not just asserted.
- **Prediction accuracy tracking.** A running predicted-vs-actual comparison
  (by gameweek, or cumulative) would be a genuinely useful sanity check on
  whether the models are well-calibrated, and would make it easy to notice
  if something regresses.
- **Discord webhook integration.** The codebase already has `DISCORD_WEBHOOK`
  support elsewhere (see `fill_transfersuggestion_table.py`) — hooking the
  dashboard's chip recommendations into that (e.g. ping when a chip flips
  from HOLD to PLAY NOW) would close the loop between "the dashboard knows"
  and "a person finds out in time to act."
- **Mini-league standings**, if `FPL_LEAGUE_ID` is configured — would need
  live API access (credentials), so lower priority until that's set up.

## Polish

- Mobile-friendly layout — currently fine on desktop, untested on phone.
- The chip-timing panel's "no squad yet" error at the very start of a
  season is a real, if narrow, upstream limitation (see
  `docs/chip_timing_spec.md` / memory notes on `get_squad_from_transactions`'s
  `gameweek < next_gw` filter) — worth fixing properly in
  `airsenal/framework/optimization_utils.py` at some point rather than
  routing around it in this dashboard, since it affects
  `airsenal_chip_report` too, not just this tool.
- Config for which FPL team to show is now env-var driven
  (`DASHBOARD_FPL_TEAM_ID`) — could go further and support tracking multiple
  teams/squads in one view if that ever becomes useful.

## Two-team A/B test: dashboard support (done 2026-08-10)

Decided to live-test greedy vs. auto (λ=0.5) with two real FPL teams this
season, since 2 seasons of replay evidence wasn't enough to call it (greedy
2072 > auto 2065 > off 1970, combined actual points — see TODO.md §1, now
superseded by the final 3-season result in docs/chip_timing_spec.md §9).
Team 1 is the existing team (742663), switched to `chip_strategy=manual`
+ all `*_week=0` (greedy) via `tools/weekly_transfer_run_team1_greedy.sh`.
Team 2 (`chip_strategy=auto`, `risk_lambda=0.5`) is scaffolded in
`tools/weekly_transfer_run_team2_auto.sh` but **still not active** — it
exits immediately until a real FPL_TEAM_ID is filled in (team 2's account
exists now, but FPL only assigns an ID once its first squad is picked on
the site — pending).

`dashboard_app.py` now shows both teams side by side, clearly labelled
(colour-coded panels, strategy shown under each team's name), each with its
own ops status/chip report/squad/predictions/suggested transfers/
injuries/fixtures, queried via an independent DB session per team
(`_team_dbsession`) rather than the module-global `session` — this needed
`get_starting_squad`/`get_squad_from_transactions`
(`airsenal/framework/optimization_utils.py`) and `Squad.get_actual_points`
to accept an explicit `dbsession` instead of hardcoding the global one (a
real, separately-useful fix — anything using those functions can now
target an arbitrary AIRSENAL_HOME without needing a process restart).

A team not yet configured (no FPL_TEAM_ID, or no DB at its AIRSENAL_HOME
yet) shows a clear pending-setup message instead of crashing or being
silently blank. Once team 2 has a real FPL_TEAM_ID:

1. Fill in `FPL_TEAM_ID_TEAM2` in `tools/weekly_transfer_run_team2_auto.sh`
   and set `DASHBOARD_TEAM2_FPL_TEAM_ID` for the dashboard process.
2. Seed a dedicated DB: `AIRSENAL_HOME=/root/airsenal_home_dashboard_team2
   uv run airsenal_setup_db`, then an initial squad build for that team.
3. Add the script to apollol's crontab (same schedule as team 1).
4. Both panels will then populate automatically — no further dashboard
   code changes needed.

Also added a "Head-to-head" summary card (predicted points, squad value,
last complete GW for both teams at a glance) and a "Simulated season
performance" section per team — **explicitly labelled as simulated, not
real** (points the suggested squad would have scored if every weekly
suggestion had been followed exactly, reconstructed from TransferSuggestion
history; simple full-15-player sum, no captain/bench modelling, since this
automation never applies anything so there's no real Transaction history to
use — see `_reconstruct_committed_squads`'s docstring). **User has said
real FPL API tracking will replace this once login credentials are
available for both accounts** — when that happens, swap
`_load_simulated_season_performance` for a real API-based history pull
(needs `FPL_LOGIN`/`FPL_PASSWORD` per team, currently disabled headless via
`fetcher._set_login_failed`).
