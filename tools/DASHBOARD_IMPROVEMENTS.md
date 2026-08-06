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
