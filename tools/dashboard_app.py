"""
Lightweight read-only ops dashboard for AIrsenal.

Not part of the AIrsenal package - a standalone ad-hoc tool (same spirit as
run_validation.py) for a quick view of, per team (see TEAM_CONFIGS below -
currently a live 2-team A/B test, greedy vs auto chip-timing):
  - ops/system status (DB freshness, latest prediction tag)
  - the chip timing report (docs/chip_timing_spec.md / airsenal_chip_report)
  - the current squad and next-gameweek predictions
  - this week's suggested transfers
  - squad injuries/availability and upcoming fixtures
  - simulated season performance (see _load_simulated_season_performance -
    NOT real FPL history, since this automation is suggestion-only; a
    stand-in until real API-based tracking is wired up once credentials
    are available - see DASHBOARD_IMPROVEMENTS.md)

Plus global (not team-specific) sections:
  - price change momentum (see airsenal/framework/price_change.py - a
    ranking, not a calibrated rise/fall prediction, see that module's
    docstring for why)
  - replay validation results (off/greedy/auto chip-strategy comparison,
    read from run_validation.py's JSON output files)

Run with: uv run python3 dashboard_app.py
"""

import contextlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template_string
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from airsenal.framework.optimization_utils import get_starting_squad
from airsenal.framework.price_change import get_price_momentum, status_label
from airsenal.framework.schema import (
    Absence,
    Fixture,
    PlayerScore,
    TransferSuggestion,
    session,
)
from airsenal.framework.squad import Squad
from airsenal.framework.utils import (
    CURRENT_SEASON,
    NEXT_GAMEWEEK,
    fetcher,
    get_last_complete_gameweek_in_db,
    get_latest_prediction_tag,
    get_player_name,
    get_predicted_points_for_player,
)
from airsenal.scripts.chip_report import build_chip_report


def _env_int(name: str) -> int | None:
    val = os.environ.get(name)
    return int(val) if val else None


# Two-team live A/B test (see TODO.md sect.1 / docs/chip_timing_spec.md
# sect.9): team 1 runs "greedy" (matching the validated replay conclusion),
# team 2 runs "auto" (risk_lambda=0.5) as a further, non-replay check on
# whether greedy really is better. Each team has its own AIRSENAL_HOME (own
# SQLite file) since they're separate FPL teams with separate squads/
# transfer history - queried here via independent sessions (see
# _team_dbsession) rather than the module-global `session`, so both can be
# shown on one page from a single process.
TEAM_CONFIGS = [
    {
        "key": "team1",
        "label": os.environ.get("DASHBOARD_TEAM1_LABEL", "Team 1"),
        "strategy": os.environ.get(
            "DASHBOARD_TEAM1_STRATEGY", "greedy (chip playable any week)"
        ),
        "fpl_team_id": (
            _env_int("DASHBOARD_TEAM1_FPL_TEAM_ID")
            or _env_int("DASHBOARD_FPL_TEAM_ID")
            or 3178353
        ),
        "airsenal_home": os.environ.get(
            "DASHBOARD_TEAM1_AIRSENAL_HOME", "/root/airsenal_home_dashboard"
        ),
    },
    {
        "key": "team2",
        "label": os.environ.get("DASHBOARD_TEAM2_LABEL", "Team 2"),
        "strategy": os.environ.get(
            "DASHBOARD_TEAM2_STRATEGY", "auto (chip_strategy=auto, λ=0.5)"
        ),
        "fpl_team_id": _env_int("DASHBOARD_TEAM2_FPL_TEAM_ID"),
        "airsenal_home": os.environ.get(
            "DASHBOARD_TEAM2_AIRSENAL_HOME", "/root/airsenal_home_dashboard_team2"
        ),
    },
]

# Where run_validation.py's --out JSON files land, and which team's DB the
# (league-wide, not team-specific) price momentum snapshots live in. Only
# results/data on the same filesystem as this process are read directly -
# results computed on other hosts (e.g. tower) need to be rsynced into this
# directory to show up here - kept deliberately simple (local file reads
# only) rather than reaching across the network from inside the dashboard.
REPLAY_RESULTS_DIR = Path(
    os.environ.get("DASHBOARD_REPLAY_RESULTS_DIR", "/root/airsenal_replay/results")
)
PRICE_SNAPSHOT_TEAM_KEY = os.environ.get("DASHBOARD_PRICE_SNAPSHOT_TEAM", "team1")

# This is a headless server process with no stdin - without FPL_LOGIN/
# FPL_PASSWORD configured, fetcher.login() would otherwise block forever on
# an interactive input() prompt the first time anything needs the live API
# (e.g. build_chip_report's use_api=True path). Pre-empt that: mark login as
# already failed so those code paths take their normal "no credentials,
# fall back to DB" route instead of prompting.
fetcher._set_login_failed(
    msg="Running headless (dashboard_app.py); skipping interactive login."
)

app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html>
<head>
<title>AIrsenal Dashboard</title>
<meta http-equiv="refresh" content="300">
<style>
body { font-family: -apple-system, Segoe UI, sans-serif; max-width: 1300px; margin: 2em auto; padding: 0 1em; background:#0b0d12; color:#e6e6e6;}
h1 { font-size:1.4em; }
h2 { margin-top:2em; border-bottom:1px solid #333; padding-bottom:.3em;}
h3 { margin-top:1.2em; }
table { border-collapse: collapse; width:100%; margin-top:.5em;}
th, td { text-align:left; padding:.4em .6em; border-bottom:1px solid #222; font-size:.9em;}
th { color:#999; font-weight:600;}
.tag { display:inline-block; padding:.15em .5em; border-radius:4px; font-size:.8em; margin-left:.5em;}
.play { background:#1e5c2e; color:#8fe3a3;}
.hold { background:#3a3a1e; color:#e3d98f;}
.ok { color:#8fe3a3; }
.warn { color:#e3d98f; }
.err { color:#e38f8f; }
.muted { color:#888; }
.card { background:#151821; border:1px solid #262a36; border-radius:8px; padding:1em; margin-bottom:1em;}
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:1em; }
.teams-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(480px,1fr)); gap:1.5em; align-items:start; }
.stat { font-size:1.6em; font-weight:600; }
.label { color:#999; font-size:.8em; text-transform:uppercase; letter-spacing:.05em;}
code { background:#1c2029; padding:.1em .4em; border-radius:4px; font-size:.85em;}
.team-panel { border-radius:10px; padding:1.2em; }
.team-panel.team1 { border:2px solid #2f6fed; background:#0f1420; }
.team-panel.team2 { border:2px solid #b06fed; background:#160f20; }
.team-title { font-size:1.2em; font-weight:700; display:flex; align-items:center; gap:.6em; }
.team-swatch { display:inline-block; width:.9em; height:.9em; border-radius:50%; }
.team1 .team-swatch, .team-swatch.team1 { background:#2f6fed; }
.team2 .team-swatch, .team-swatch.team2 { background:#b06fed; }
.team-strategy { color:#aaa; font-size:.9em; margin:.2em 0 1em 0; }
.pending-card { border:1px dashed #444; border-radius:8px; padding:1.5em; color:#999; text-align:center; }
.tabs { display:flex; gap:.3em; margin:1.5em 0 0 0; border-bottom:1px solid #333; }
.tab-btn { background:none; border:none; color:#999; padding:.6em 1.3em; font-size:1em; cursor:pointer; border-bottom:2px solid transparent; font-family:inherit; }
.tab-btn:hover { color:#ccc; }
.tab-btn.active { color:#e6e6e6; font-weight:600; }
.tab-btn[data-tab="team1"].active { border-bottom-color:#2f6fed; }
.tab-btn[data-tab="team2"].active { border-bottom-color:#b06fed; }
.tab-btn[data-tab="general"].active { border-bottom-color:#8fe3a3; }
.tab-panel { display:none; }
.tab-panel.active { display:block; }
</style>
</head>
<body>
<h1>AIrsenal Dashboard <span class="muted" style="font-size:.5em">generated {{ now }} (auto-refreshes every 5 min)</span></h1>

<div class="tabs">
  <button class="tab-btn" data-tab="general" onclick="showTab('general')">General</button>
  {% for team in teams %}
  <button class="tab-btn" data-tab="{{ team.key }}" onclick="showTab('{{ team.key }}')">{{ team.label }}</button>
  {% endfor %}
</div>

<div id="tab-general" class="tab-panel">

<h2>Season status</h2>
<div class="grid">
  {% for stat in ops_stats %}
  <div class="card">
    <div class="label">{{ stat.label }}</div>
    <div class="stat {{ stat.cls }}">{{ stat.value }}</div>
    {% if stat.note %}<div class="muted" style="font-size:.8em">{{ stat.note }}</div>{% endif %}
  </div>
  {% endfor %}
</div>

<h2>Head-to-head</h2>
<p class="muted">Live 2-team A/B test of chip-timing strategy - see docs/chip_timing_spec.md &sect;9 for why (3-season replay favoured greedy, but the margin over auto was close enough to also want a live check).</p>
<div class="teams-grid">
{% for team in teams %}
<div class="team-panel {{ team.key }}">
  <div class="team-title"><span class="team-swatch"></span>{{ team.label }}</div>
  <div class="team-strategy">{{ team.strategy }}{% if team.fpl_team_id %} &middot; FPL_TEAM_ID {{ team.fpl_team_id }}{% endif %}</div>
  {% if not team.configured %}
  <div class="pending-card">{{ team.not_configured_reason }}</div>
  {% else %}
  <div class="grid">
    <div class="card">
      <div class="label">Next GW predicted (best XI)</div>
      <div class="stat {{ 'ok' if team.squad_predicted_total is not none else 'muted' }}">{{ team.squad_predicted_total if team.squad_predicted_total is not none else '-' }}</div>
    </div>
    <div class="card">
      <div class="label">Squad value</div>
      <div class="stat {{ 'ok' if team.squad_value is not none else 'muted' }}">{% if team.squad_value is not none %}&pound;{{ team.squad_value }}m{% else %}-{% endif %}</div>
    </div>
    <div class="card">
      <div class="label">Last complete GW</div>
      <div class="stat ok">{{ team.last_complete_gw or '-' }}</div>
    </div>
  </div>
  {% endif %}
  <div class="muted" style="font-size:.85em; margin-top:.5em">See the {{ team.label }} tab above for full detail.</div>
</div>
{% endfor %}
</div>

<h2>Price change momentum</h2>
<p class="muted">Net transfers since the last snapshot, ranked - a <b>momentum indicator, not a calibrated rise/fall prediction</b>. Backtesting a simple threshold against real historical data found weak precision (see airsenal/framework/price_change.py's docstring) - treat this as "who's moving", not "who will change price". League-wide, not team-specific - sourced from {{ price_snapshot_team_label }}'s database (wherever the price-snapshot cron writes).</p>
{% if price_momentum_error %}
<p class="err">{{ price_momentum_error }}</p>
{% elif not price_movers %}
<p class="muted">No momentum data yet - needs at least two daily snapshots (see tools/price_change_snapshot_run.sh, runs every 4 hours). Check back once that's been running for a day or more.</p>
{% else %}
<table>
<tr><th>Player</th><th>Price</th><th>Net transfers</th><th>Momentum</th><th>Owned by</th><th>Status</th></tr>
{% for p in price_movers %}
<tr><td>{{ p.name }}{% if p.in_squad_keys %} <span class="tag play">{{ p.in_squad_keys|join(', ')|upper }}</span>{% endif %}</td><td>&pound;{{ p.price / 10 }}m</td><td>{{ "%+d"|format(p.net_transfers_today) }}</td><td>{{ "%+.2f"|format(p.momentum_pct) }}%</td><td>{{ p.selected_by_percent }}%</td><td class="muted">{{ p.status }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h2>Replay validation results</h2>
{% if not replay_by_season %}
<p class="muted">No replay results found yet.</p>
{% else %}
<p class="muted">off/greedy/auto chip-strategy comparison from season replays (see docs/chip_timing_spec.md &sect;4.4/&sect;9). "greedy" = chips playable any week; "auto" = the decision-rule recommender at the given &lambda;.</p>
<table>
<tr><th>Season</th>{% for c in replay_configs %}<th>{{ c }}</th>{% endfor %}</tr>
{% for season, rows in replay_by_season.items()|sort %}
<tr><td>{{ season }}</td>
{% for c in replay_configs %}
{% if c in rows %}<td>{{ rows[c].total_actual_points }}<span class="muted"> ({{ "%.0f"|format(rows[c].total_expected_points) }} pred)</span></td>
{% else %}<td class="muted">-</td>{% endif %}
{% endfor %}
</tr>
{% endfor %}
<tr style="border-top:2px solid #444"><td><b>Combined</b></td>
{% for c in replay_configs %}
{% if c in replay_combined %}
<td><b class="{{ 'ok' if c == best_combined_config else '' }}">{{ replay_combined[c].actual }}</b>
{% if replay_combined[c].seasons < replay_by_season|length %}<span class="muted"> ({{ replay_combined[c].seasons }}/{{ replay_by_season|length }} seasons)</span>{% endif %}
</td>
{% else %}<td class="muted">-</td>{% endif %}
{% endfor %}
</tr>
</table>
<p class="muted" style="font-size:.85em">All runs use reduced-fidelity settings (num_iterations=15, weeks_ahead=2) for tractability - see docs/chip_timing_spec.md &sect;9 for the full methodology, caveats, and final conclusion.</p>
{% endif %}

</div>

{% for team in teams %}
<div id="tab-{{ team.key }}" class="tab-panel">
<h2><span class="team-swatch {{ team.key }}"></span> {{ team.label }} <span class="muted" style="font-size:.6em">{{ team.strategy }}</span></h2>
{% if not team.configured %}
<div class="pending-card">{{ team.not_configured_reason }}</div>
{% else %}

<h3>Chip timing report</h3>
{% if team.chip_error %}
<p class="err">{{ team.chip_error }}</p>
{% else %}
<p class="muted">Season {{ team.chip_report.season }} &middot; next GW{{ team.chip_report.next_gw }} &middot; tag <code>{{ team.chip_report.tag }}</code> &middot; risk_lambda={{ team.chip_report.risk_lambda }}</p>
{% for chip_name, chip in team.chip_report.chips.items() %}
<div class="card">
  <b>{{ chip_name.replace('_',' ')|title }}</b>
  <span class="muted">window GW{{ chip.window.start_gw }}-{{ chip.window.end_gw }}
  {% if chip.window.available %}(available){% else %}(not available){% endif %}</span>
  {% if chip.recommendation %}
  <span class="tag {{ 'play' if chip.recommendation.play_now else 'hold' }}">
    {{ 'PLAY NOW' if chip.recommendation.play_now else 'HOLD' }} - best GW{{ chip.recommendation.best_gw }}
  </span>
  <table>
    <tr><th>GW</th><th>Expected gain</th><th>Method</th><th>Notes</th></tr>
    {# chip is a plain dict, so chip.values would resolve to the dict's
       .values method rather than the key - subscript it explicitly. #}
    {% for v in chip['values'][:8] %}
    <tr><td>GW{{ v.gameweek }}</td><td>+{{ v.expected_gain }}</td><td>{{ v.method }}</td>
        <td class="muted">{{ v.notes }}{% if v.double_teams %} DGW: {{ v.double_teams|join(', ') }}{% endif %}</td></tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">Not currently available.</p>
  {% endif %}
</div>
{% endfor %}
{% endif %}

<h3>Squad &amp; predictions (GW{{ team.next_gw }})</h3>
{% if team.squad_error %}
<p class="err">{{ team.squad_error }}</p>
{% else %}
<p class="muted">Total predicted (best XI): <b>{{ team.squad_predicted_total }}</b> pts</p>
<table>
<tr><th>Player</th><th>Pos</th><th>Team</th><th>Price</th><th>Predicted GW{{ team.next_gw }}</th></tr>
{% for p in team.squad_players %}
<tr><td>{{ p.name }}</td><td>{{ p.position }}</td><td>{{ p.team }}</td><td>&pound;{{ p.price }}m</td><td>{{ p.predicted }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h3>This week's suggested transfers</h3>
{% if not team.suggested_transfers %}
<p class="muted">No suggestion found yet - the weekly automated run writes one every Thursday, or run it manually.</p>
{% else %}
<p class="muted">From the weekly automated run ({{ team.strategy }}) &middot; computed {{ team.suggestion_timestamp }} &middot; suggestion only, nothing applied to the live team.</p>
{% if team.suggestion_is_new_squad %}
<p class="muted">This is the initial squad build (no transfer history yet) - {{ team.suggested_transfers[1]|length }} players.</p>
{% else %}
{% for gw, moves in team.suggested_transfers.items()|sort %}
<div class="card">
  <b>GW{{ gw }}</b>
  {% if team.suggestion_chip.get(gw) %}<span class="tag play">{{ team.suggestion_chip[gw]|upper }}</span>{% endif %}
  <span class="muted"> &middot; {{ "%+.1f"|format(team.suggestion_gain) }} pts vs. no transfers</span>
  <table>
    <tr><th>Out</th><th>In</th></tr>
    {% for out_name, in_name in moves %}
    <tr><td>{{ out_name or '-' }}</td><td>{{ in_name or '-' }}</td></tr>
    {% endfor %}
  </table>
</div>
{% endfor %}
{% endif %}
{% endif %}

<h3>Squad injuries &amp; availability</h3>
{% if not team.absences %}
<p class="muted">No current injuries/suspensions affecting the squad.</p>
{% else %}
<table>
<tr><th>Player</th><th>Reason</th><th>Details</th><th>Expected back</th></tr>
{% for a in team.absences %}
<tr><td>{{ a.name }}</td><td>{{ a.reason }}</td><td class="muted">{{ a.details or '-' }}</td><td>{{ a.expected_back }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h3>Upcoming fixtures</h3>
{% if not team.upcoming_fixtures %}
<p class="muted">No fixture data available.</p>
{% else %}
<table>
<tr><th>Team</th>{% for gw in team.fixture_gameweeks %}<th>GW{{ gw }}</th>{% endfor %}</tr>
{% for tname, gws in team.upcoming_fixtures.items()|sort %}
<tr><td>{{ tname }}</td>
{% for gw in team.fixture_gameweeks %}
<td>{{ gws.get(gw, ['-'])|join(', ') }}</td>
{% endfor %}
</tr>
{% endfor %}
</table>
{% endif %}

<h3>Simulated season performance</h3>
<p class="muted">Points {{ team.label }}'s suggested squad would have scored <b>if every weekly suggestion had been followed exactly</b> - a simulation, not real FPL history (this automation is suggestion-only, see tools/weekly_transfer_run_{{ team.key }}_*.sh). Simple sum across the full 15-player squad (no captain doubling or bench/sub rules, since which exact lineup was chosen historically isn't tracked) - a rough proxy, not a polished score. Will be replaced with real FPL API history once credentials are available.</p>
{% if team.performance_error %}
<p class="muted">{{ team.performance_error }}</p>
{% elif not team.performance_rows %}
<p class="muted">No completed gameweeks yet this season.</p>
{% else %}
<table>
<tr><th>GW</th><th>Points (simulated)</th><th>Cumulative</th></tr>
{% for row in team.performance_rows %}
<tr><td>GW{{ row.gameweek }}</td><td>{{ row.points }}</td><td><b>{{ row.cumulative }}</b></td></tr>
{% endfor %}
</table>
{% endif %}

{% endif %}
</div>
{% endfor %}

<script>
function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(function (el) { el.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function (el) { el.classList.remove('active'); });
  var panel = document.getElementById('tab-' + name);
  var btn = document.querySelector('.tab-btn[data-tab="' + name + '"]');
  if (!panel || !btn) { name = 'general'; panel = document.getElementById('tab-general'); btn = document.querySelector('.tab-btn[data-tab="general"]'); }
  panel.classList.add('active');
  btn.classList.add('active');
  localStorage.setItem('airsenal_dashboard_tab', name);
}
// page auto-refreshes every 5 min (see <meta refresh> above) - restore
// whichever tab was open rather than always jumping back to General.
showTab(localStorage.getItem('airsenal_dashboard_tab') || 'general');
</script>

</body>
</html>
"""


def _team_dbsession(airsenal_home: str):
    """An independent DB session for the given AIRSENAL_HOME directory, or
    None if it doesn't have a database yet (team not set up). Deliberately
    a separate engine/session from the module-global `session` (which is
    bound to whichever AIRSENAL_HOME this process itself started with) -
    see the get_starting_squad/get_squad_from_transactions dbsession fix
    this relies on (airsenal/framework/optimization_utils.py)."""
    db_file = Path(airsenal_home) / "data.db"
    if not db_file.exists():
        return None
    engine = create_engine(f"sqlite:///{db_file}")
    return sessionmaker(bind=engine, autoflush=False)()


def _load_suggested_transfers(fpl_team_id, season, dbsession):
    """Latest batch of TransferSuggestion rows for this team/season, paired
    into (out, in) moves per gameweek. Returns
    (moves_by_gw, chip_by_gw, points_gain, timestamp, is_new_squad)."""
    latest = dbsession.scalars(
        select(TransferSuggestion)
        .where(
            TransferSuggestion.fpl_team_id == fpl_team_id,
            TransferSuggestion.season == season,
        )
        .order_by(TransferSuggestion.timestamp.desc())
        .limit(1)
    ).first()
    if latest is None:
        return {}, {}, 0.0, None, False

    rows = dbsession.scalars(
        select(TransferSuggestion).where(
            TransferSuggestion.fpl_team_id == fpl_team_id,
            TransferSuggestion.season == season,
            TransferSuggestion.timestamp == latest.timestamp,
        )
    ).all()

    ins_by_gw: dict[int, list[str]] = {}
    outs_by_gw: dict[int, list[str]] = {}
    chip_by_gw: dict[int, str | None] = {}
    for r in rows:
        name = get_player_name(r.player_id, dbsession)
        target = ins_by_gw if r.in_or_out == 1 else outs_by_gw
        target.setdefault(r.gameweek, []).append(name)
        chip_by_gw[r.gameweek] = r.chip_played

    is_new_squad = not outs_by_gw and any(len(v) > 4 for v in ins_by_gw.values())
    moves_by_gw = {}
    if not is_new_squad:
        for gw in sorted(set(ins_by_gw) | set(outs_by_gw)):
            ins = ins_by_gw.get(gw, [])
            outs = outs_by_gw.get(gw, [])
            n = max(len(ins), len(outs))
            moves_by_gw[gw] = [
                (outs[i] if i < len(outs) else None, ins[i] if i < len(ins) else None)
                for i in range(n)
            ]
    else:
        moves_by_gw = {1: [name for names in ins_by_gw.values() for name in names]}

    return (
        moves_by_gw,
        chip_by_gw,
        latest.points_gain,
        latest.timestamp,
        is_new_squad,
    )


def _load_absences(squad_player_ids, season, next_gw, dbsession):
    """Injuries/suspensions for the current squad that are still active (no
    end gameweek, or one in the future)."""
    rows = dbsession.scalars(
        select(Absence).where(
            Absence.player_id.in_(squad_player_ids),
            Absence.season == season,
        )
    ).all()
    out = []
    for a in rows:
        if a.gw_until is not None and a.gw_until < next_gw:
            continue  # already over
        out.append(
            {
                "name": a.player.name if a.player else str(a.player_id),
                "reason": a.reason,
                "details": a.details,
                "expected_back": f"GW{a.gw_until}" if a.gw_until else "unknown",
            }
        )
    return out


def _load_upcoming_fixtures(squad_teams, season, next_gw, dbsession, n_weeks=5):
    """{team: {gameweek: [opponent strings]}} for the given teams over the
    next n_weeks gameweeks."""
    gw_range = list(range(next_gw, next_gw + n_weeks))
    fixtures = dbsession.scalars(
        select(Fixture).where(
            Fixture.season == season,
            Fixture.gameweek.in_(gw_range),
        )
    ).all()
    by_team: dict[str, dict[int, list[str]]] = {t: {} for t in squad_teams}
    for f in fixtures:
        if f.home_team in by_team:
            by_team[f.home_team].setdefault(f.gameweek, []).append(f"{f.away_team} (H)")
        if f.away_team in by_team:
            by_team[f.away_team].setdefault(f.gameweek, []).append(f"{f.home_team} (A)")
    return by_team, gw_range


def _load_price_movers(season, squad_player_ids_by_team, dbsession, top_n=10):
    """Top N risers and fallers by momentum (see get_price_momentum), each
    flagged with which team(s)' squad(s) it's in."""
    results = get_price_momentum(season=season, dbsession=dbsession)
    if not results:
        return []
    movers = results[:top_n] + list(reversed(results[-top_n:]))
    seen = set()
    out = []
    for r in movers:
        if r.player_id in seen:
            continue
        seen.add(r.player_id)
        out.append(
            {
                "name": get_player_name(r.player_id, dbsession),
                "price": r.price,
                "net_transfers_today": r.net_transfers_today,
                "momentum_pct": r.momentum_pct,
                "selected_by_percent": r.selected_by_percent,
                "status": status_label(r.momentum_pct),
                "in_squad_keys": [
                    key
                    for key, ids in squad_player_ids_by_team.items()
                    if r.player_id in ids
                ],
            }
        )
    return sorted(out, key=lambda p: p["momentum_pct"], reverse=True)


def _reconstruct_committed_squads(
    fpl_team_id: int, season: str, dbsession, up_to_gw: int
) -> tuple[dict[int, list[int]], str | None]:
    """Reconstruct the squad AIrsenal's suggestions imply for each
    gameweek, from the full TransferSuggestion history for this team - a
    SIMULATION of what would exist if every weekly suggestion had been
    followed exactly, not real history (this automation never applies
    anything - see tools/weekly_transfer_run_*.sh).

    Each weekly run's TransferSuggestion batch (grouped by timestamp) can
    cover several gameweeks ahead (its look-ahead horizon), but only the
    *nearest* gameweek in each batch is ever actually "committed" - like a
    real manager, who only acts on the coming gameweek's decision, with
    later weeks reconsidered fresh by the following run.

    Known limitation: doesn't model free-hit reversion (a free-hit
    gameweek's transfers are treated as permanent here, same as any other
    transfer) - a rare edge case not worth the complexity for what's meant
    to be a rough, temporary stand-in (see module docstring).

    Returns ({gameweek: [player_id, ...]}, error). error is a message
    string (and the dict is empty) if there's no suggestion history at all.
    """
    rows = dbsession.scalars(
        select(TransferSuggestion)
        .where(
            TransferSuggestion.fpl_team_id == fpl_team_id,
            TransferSuggestion.season == season,
        )
        .order_by(TransferSuggestion.timestamp, TransferSuggestion.gameweek)
    ).all()
    if not rows:
        return {}, "No suggestion history yet - nothing to simulate."

    batches: dict[str, list[TransferSuggestion]] = {}
    for r in rows:
        batches.setdefault(r.timestamp, []).append(r)

    squad = Squad(season=season)
    squad_by_gw: dict[int, list[int]] = {}
    committed_gws: set[int] = set()
    for timestamp in sorted(batches):
        batch = batches[timestamp]
        nearest_gw = min(r.gameweek for r in batch)
        if nearest_gw > up_to_gw or nearest_gw in committed_gws:
            continue
        for r in batch:
            if r.gameweek != nearest_gw:
                continue
            if r.in_or_out == -1:
                squad.remove_player(r.player_id, dbsession=dbsession)
            else:
                squad.add_player(
                    r.player_id,
                    gameweek=nearest_gw,
                    check_budget=False,
                    check_team=False,
                    dbsession=dbsession,
                )
        committed_gws.add(nearest_gw)
        squad_by_gw[nearest_gw] = [p.player_id for p in squad.players]

    return squad_by_gw, None


def _load_simulated_season_performance(
    fpl_team_id, season, dbsession, last_complete_gw
):
    """See _reconstruct_committed_squads. Returns (rows, error) where rows
    is [{"gameweek", "points", "cumulative"}, ...] for every completed
    gameweek from the first committed squad onward (forward-filling
    gameweeks with no new suggestion batch - the squad just carries over
    unchanged), or ([], error/None) if there's nothing to show yet."""
    if not last_complete_gw:
        return [], None

    committed, error = _reconstruct_committed_squads(
        fpl_team_id, season, dbsession, last_complete_gw
    )
    if error:
        return [], error

    rows = []
    cumulative = 0
    current_squad_ids: list[int] | None = None
    for gw in range(1, last_complete_gw + 1):
        if gw in committed:
            current_squad_ids = committed[gw]
        if current_squad_ids is None:
            continue
        pts = 0
        for player_id in current_squad_ids:
            scores = dbsession.scalars(
                select(PlayerScore)
                .join(Fixture, PlayerScore.fixture_id == Fixture.fixture_id)
                .where(
                    Fixture.season == season,
                    Fixture.gameweek == gw,
                    PlayerScore.player_id == player_id,
                )
            ).all()
            pts += sum(s.points for s in scores)
        cumulative += pts
        rows.append({"gameweek": gw, "points": pts, "cumulative": cumulative})

    if not rows:
        return [], "No completed gameweeks with a committed squad yet."
    return rows, None


def _load_next_deadline(next_gw: int) -> tuple[str | None, str | None]:
    """Transfer deadline for the given gameweek, from the public
    bootstrap-static endpoint (no login needed - see
    FPLDataFetcher.get_event_data). Returns (formatted_value, note) - both
    None if it couldn't be fetched (e.g. no network from this host)."""
    try:
        event_data = fetcher.get_event_data()
        deadline_raw = event_data.get(next_gw, {}).get("deadline")
        if not deadline_raw:
            return None, "Not published yet"
        deadline = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
        now_utc = datetime.now(deadline.tzinfo)
        delta = deadline - now_utc
        if delta.total_seconds() <= 0:
            return deadline.strftime("%a %d %b %H:%M UTC"), "Passed"
        days, hours = delta.days, delta.seconds // 3600
        countdown = (
            f"{days}d {hours}h" if days else f"{hours}h {(delta.seconds % 3600) // 60}m"
        )
        return deadline.strftime("%a %d %b %H:%M UTC"), f"in {countdown}"
    except Exception as e:
        # best-effort informational stat (e.g. no network from this host) -
        # any failure just hides the value rather than breaking the page
        return None, str(e)


def _tmux_replay_status():
    try:
        out = subprocess.run(
            ["tmux", "list-windows", "-t", "airsenal_lambda"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode != 0:
            return "no active session", "muted", None
        lines = [line for line in out.stdout.splitlines() if line.strip()]
        active = [line for line in lines if "control" not in line]
        return f"{len(active)} windows", "ok", "\n".join(active[:8])
    except (subprocess.SubprocessError, OSError) as e:
        return "error", "err", str(e)


def _build_team_context(team: dict) -> dict:
    """All template context for one team's panel - see TEAM_CONFIGS."""
    ctx: dict = {
        "key": team["key"],
        "label": team["label"],
        "strategy": team["strategy"],
        "fpl_team_id": team["fpl_team_id"],
        "configured": True,
        "not_configured_reason": None,
        "squad_player_ids": [],
        "last_complete_gw": None,
        "squad_predicted_total": None,
        "squad_value": None,
        "next_gw": NEXT_GAMEWEEK,
        "squad_players": [],
        "squad_error": None,
        "chip_report": None,
        "chip_error": None,
        "suggested_transfers": {},
        "suggestion_chip": {},
        "suggestion_gain": 0.0,
        "suggestion_timestamp": None,
        "suggestion_is_new_squad": False,
        "absences": [],
        "upcoming_fixtures": {},
        "fixture_gameweeks": [],
        "performance_rows": [],
        "performance_error": None,
    }
    if not team["fpl_team_id"]:
        ctx["configured"] = False
        ctx["not_configured_reason"] = (
            "No FPL_TEAM_ID set yet - FPL only assigns one once you've "
            "picked your first squad on the site. Set "
            f"DASHBOARD_{team['key'].upper()}_FPL_TEAM_ID once you have it, "
            f"and fill it into tools/weekly_transfer_run_{team['key']}_*.sh."
        )
        return ctx

    dbsession = _team_dbsession(team["airsenal_home"])
    if dbsession is None:
        ctx["configured"] = False
        ctx["not_configured_reason"] = (
            f"No database found yet at {team['airsenal_home']} - needs "
            "fill_db_init and an initial squad build for this team first."
        )
        return ctx

    fpl_team_id = team["fpl_team_id"]

    last_complete_gw = None
    with contextlib.suppress(ValueError, RuntimeError):
        last_complete_gw = get_last_complete_gameweek_in_db(CURRENT_SEASON, dbsession)
    ctx["last_complete_gw"] = last_complete_gw

    latest_tag = None
    with contextlib.suppress(RuntimeError):
        latest_tag = get_latest_prediction_tag(
            season=CURRENT_SEASON, dbsession=dbsession
        )

    chip_report = None
    chip_error = None
    try:
        # NOTE: build_chip_report reconstructs the squad via the same
        # gameweek < next_gw logic noted below - at the very start of a
        # season (NEXT_GAMEWEEK == 1) that excludes the GW1 squad-selection
        # transactions themselves, so this panel will show a "no squad yet"
        # error for the first gameweek only. Resolves itself automatically
        # once NEXT_GAMEWEEK advances past 1.
        chip_report = build_chip_report(
            fpl_team_id, season=CURRENT_SEASON, dbsession=dbsession
        )
    except (ValueError, RuntimeError) as e:
        chip_error = str(e)
    ctx["chip_report"] = chip_report
    ctx["chip_error"] = chip_error

    squad_players: list[dict] = []
    squad_error = None
    squad_predicted_total = None
    squad_value = None
    squad = None
    next_gw = NEXT_GAMEWEEK
    ctx["next_gw"] = next_gw
    try:
        tag = latest_tag or get_latest_prediction_tag(
            season=CURRENT_SEASON, dbsession=dbsession
        )
        # get_starting_squad reconstructs from transactions with
        # gameweek < next_gw, so at the very start of a season (NEXT_GAMEWEEK
        # == 1) the GW1 "buy" transactions themselves would be excluded -
        # request gameweek 2 instead so they're included; the squad hasn't
        # changed since, so this is still the right squad to display.
        squad = get_starting_squad(
            next_gw=max(next_gw, 2),
            season=CURRENT_SEASON,
            fpl_team_id=fpl_team_id,
            use_api=False,
            dbsession=dbsession,
        )
        for p in squad.players:
            preds = get_predicted_points_for_player(
                p.player_id, tag, season=CURRENT_SEASON, dbsession=dbsession
            )
            squad_players.append(
                {
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "price": round(p.purchase_price / 10, 1),
                    "predicted": round(preds.get(next_gw, 0), 1),
                }
            )
        position_order = ["GK", "DEF", "MID", "FWD"]
        squad_players.sort(
            key=lambda x: (position_order.index(x["position"]), -x["predicted"])
        )
        squad_predicted_total = round(squad.get_expected_points(next_gw, tag), 1)
        squad_value = round(sum(p.purchase_price for p in squad.players) / 10, 1)
    except (ValueError, RuntimeError) as e:
        squad_error = str(e)
    ctx["squad_players"] = squad_players
    ctx["squad_error"] = squad_error
    ctx["squad_predicted_total"] = squad_predicted_total
    ctx["squad_value"] = squad_value

    try:
        (
            suggested_transfers,
            suggestion_chip,
            suggestion_gain,
            suggestion_timestamp,
            suggestion_is_new_squad,
        ) = _load_suggested_transfers(fpl_team_id, CURRENT_SEASON, dbsession)
    except (ValueError, RuntimeError) as e:
        suggested_transfers, suggestion_chip, suggestion_gain = {}, {}, 0.0
        suggestion_timestamp, suggestion_is_new_squad = f"error: {e}", False
    ctx["suggested_transfers"] = suggested_transfers
    ctx["suggestion_chip"] = suggestion_chip
    ctx["suggestion_gain"] = suggestion_gain
    ctx["suggestion_timestamp"] = suggestion_timestamp
    ctx["suggestion_is_new_squad"] = suggestion_is_new_squad

    absences: list[dict] = []
    upcoming_fixtures: dict = {}
    fixture_gameweeks: list[int] = []
    if squad_players and squad is not None:
        squad_player_ids = [p.player_id for p in squad.players]
        ctx["squad_player_ids"] = squad_player_ids
        try:
            absences = _load_absences(
                squad_player_ids, CURRENT_SEASON, next_gw, dbsession
            )
        except (ValueError, RuntimeError):
            absences = []
        try:
            squad_teams = {p["team"] for p in squad_players}
            upcoming_fixtures, fixture_gameweeks = _load_upcoming_fixtures(
                squad_teams, CURRENT_SEASON, next_gw, dbsession
            )
        except (ValueError, RuntimeError):
            upcoming_fixtures, fixture_gameweeks = {}, []
    ctx["absences"] = absences
    ctx["upcoming_fixtures"] = upcoming_fixtures
    ctx["fixture_gameweeks"] = fixture_gameweeks

    try:
        performance_rows, performance_error = _load_simulated_season_performance(
            fpl_team_id, CURRENT_SEASON, dbsession, last_complete_gw
        )
    except (ValueError, RuntimeError) as e:
        performance_rows, performance_error = [], str(e)
    ctx["performance_rows"] = performance_rows
    ctx["performance_error"] = performance_error

    return ctx


def _load_replay_results():
    """Read every run_validation.py JSON result file and pivot into
    {season: {config: row}}, plus combined (summed) totals per config across
    all seasons that have a result for it."""
    if not REPLAY_RESULTS_DIR.is_dir():
        return {}, {}

    by_season: dict[str, dict[str, dict]] = {}
    for f in sorted(REPLAY_RESULTS_DIR.glob("*.json")):
        try:
            entries = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for row in entries:
            if "error" in row:
                continue
            season = row.get("season")
            config = row.get("config")
            if not season or not config:
                continue
            by_season.setdefault(season, {})[config] = row

    combined: dict[str, dict] = {}
    for season_rows in by_season.values():
        for config, row in season_rows.items():
            c = combined.setdefault(
                config, {"actual": 0, "expected": 0.0, "seasons": 0}
            )
            c["actual"] += row["total_actual_points"]
            c["expected"] += row["total_expected_points"]
            c["seasons"] += 1

    return by_season, combined


@app.route("/")
def index():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    teams = [_build_team_context(team) for team in TEAM_CONFIGS]

    ops_stats = []
    try:
        last_complete_gw = get_last_complete_gameweek_in_db(CURRENT_SEASON, session)
        ops_stats.append(
            {"label": "Last complete GW", "value": last_complete_gw or "-", "cls": "ok"}
        )
    except (ValueError, RuntimeError) as e:
        ops_stats.append(
            {
                "label": "Last complete GW",
                "value": "error",
                "cls": "err",
                "note": str(e),
            }
        )
    ops_stats.append({"label": "Next GW", "value": NEXT_GAMEWEEK, "cls": "ok"})
    ops_stats.append({"label": "Season", "value": CURRENT_SEASON, "cls": "ok"})

    deadline_value, deadline_note = _load_next_deadline(NEXT_GAMEWEEK)
    ops_stats.append(
        {
            "label": f"GW{NEXT_GAMEWEEK} deadline",
            "value": deadline_value or "-",
            "cls": "ok" if deadline_value else "muted",
            "note": deadline_note,
        }
    )

    replay_status, replay_cls, replay_note = _tmux_replay_status()
    ops_stats.append(
        {
            "label": "Replay tmux session",
            "value": replay_status,
            "cls": replay_cls,
            "note": replay_note,
        }
    )

    # price momentum: league-wide (not team-specific), sourced from
    # whichever team's DB actually has the price-snapshot cron pointed at
    # it (see PRICE_SNAPSHOT_TEAM_KEY) - falls back gracefully if that
    # team isn't configured/doesn't have a DB yet.
    price_snapshot_team = next(
        (t for t in TEAM_CONFIGS if t["key"] == PRICE_SNAPSHOT_TEAM_KEY),
        TEAM_CONFIGS[0],
    )
    price_movers = []
    price_momentum_error = None
    price_dbsession = _team_dbsession(price_snapshot_team["airsenal_home"])
    if price_dbsession is None:
        price_momentum_error = (
            f"{price_snapshot_team['label']}'s database isn't set up yet."
        )
    else:
        squad_player_ids_by_team = {t["key"]: set(t["squad_player_ids"]) for t in teams}
        try:
            price_movers = _load_price_movers(
                CURRENT_SEASON, squad_player_ids_by_team, price_dbsession
            )
        except (ValueError, RuntimeError) as e:
            price_momentum_error = str(e)

    replay_by_season, replay_combined = _load_replay_results()
    replay_configs = sorted(
        {c for rows in replay_by_season.values() for c in rows},
        key=lambda c: (c not in ("off", "greedy"), c),
    )
    best_combined_config = (
        max(replay_combined, key=lambda c: replay_combined[c]["actual"])
        if replay_combined
        else None
    )

    return render_template_string(
        TEMPLATE,
        now=now,
        teams=teams,
        ops_stats=ops_stats,
        price_snapshot_team_label=price_snapshot_team["label"],
        price_movers=price_movers,
        price_momentum_error=price_momentum_error,
        replay_by_season=replay_by_season,
        replay_combined=replay_combined,
        replay_configs=replay_configs,
        best_combined_config=best_combined_config,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("DASHBOARD_PORT", "5050")),
        debug=False,
    )
