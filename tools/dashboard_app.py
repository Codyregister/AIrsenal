"""
Lightweight read-only ops dashboard for AIrsenal.

Not part of the AIrsenal package - a standalone ad-hoc tool (same spirit as
run_validation.py) for a quick view of:
  - ops/system status (DB freshness, latest prediction tag, replay job status)
  - the chip timing report (docs/chip_timing_spec.md / airsenal_chip_report)
  - the current squad and next-gameweek predictions
  - replay validation results (off/greedy/auto chip-strategy comparison,
    read from run_validation.py's JSON output files)
  - price change momentum (see airsenal/framework/price_change.py - a
    ranking, not a calibrated rise/fall prediction, see that module's
    docstring for why)

Run with: uv run python3 dashboard_app.py
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template_string
from sqlalchemy import select

from airsenal.framework.optimization_utils import get_starting_squad
from airsenal.framework.price_change import get_price_momentum, status_label
from airsenal.framework.schema import Absence, Fixture, TransferSuggestion, session
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

FPL_TEAM_ID = int(os.environ.get("DASHBOARD_FPL_TEAM_ID", "742663"))
# Where run_validation.py's --out JSON files land. Only results on the same
# filesystem as this process are read directly; results computed on other
# hosts (e.g. tower) need to be rsynced into this directory to show up here -
# kept deliberately simple (local file reads only) rather than reaching
# across the network from inside the dashboard.
REPLAY_RESULTS_DIR = Path(
    os.environ.get("DASHBOARD_REPLAY_RESULTS_DIR", "/root/airsenal_replay/results")
)

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
body { font-family: -apple-system, Segoe UI, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; background:#0b0d12; color:#e6e6e6;}
h1 { font-size:1.4em; }
h2 { margin-top:2em; border-bottom:1px solid #333; padding-bottom:.3em;}
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
.stat { font-size:1.6em; font-weight:600; }
.label { color:#999; font-size:.8em; text-transform:uppercase; letter-spacing:.05em;}
code { background:#1c2029; padding:.1em .4em; border-radius:4px; font-size:.85em;}
</style>
</head>
<body>
<h1>AIrsenal Dashboard <span class="muted" style="font-size:.5em">generated {{ now }} (auto-refreshes every 5 min)</span></h1>

<h2>Ops status</h2>
<div class="grid">
  {% for stat in ops_stats %}
  <div class="card">
    <div class="label">{{ stat.label }}</div>
    <div class="stat {{ stat.cls }}">{{ stat.value }}</div>
    {% if stat.note %}<div class="muted" style="font-size:.8em">{{ stat.note }}</div>{% endif %}
  </div>
  {% endfor %}
</div>

<h2>Chip timing report</h2>
{% if chip_error %}
<p class="err">{{ chip_error }}</p>
{% else %}
<p class="muted">Season {{ chip_report.season }} &middot; next GW{{ chip_report.next_gw }} &middot; tag <code>{{ chip_report.tag }}</code> &middot; risk_lambda={{ chip_report.risk_lambda }}</p>
{% for chip_name, chip in chip_report.chips.items() %}
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
    {% for v in chip.values[:8] %}
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

<h2>Squad &amp; predictions (GW{{ next_gw }})</h2>
{% if squad_error %}
<p class="err">{{ squad_error }}</p>
{% else %}
<p class="muted">Total predicted (best XI): <b>{{ squad_predicted_total }}</b> pts</p>
<table>
<tr><th>Player</th><th>Pos</th><th>Team</th><th>Price</th><th>Predicted GW{{ next_gw }}</th></tr>
{% for p in squad_players %}
<tr><td>{{ p.name }}</td><td>{{ p.position }}</td><td>{{ p.team }}</td><td>&pound;{{ p.price }}m</td><td>{{ p.predicted }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h2>This week's suggested transfers</h2>
{% if not suggested_transfers %}
<p class="muted">No suggestion found yet - the weekly automated run (see tools/weekly_transfer_run.sh) writes one every Thursday, or run it manually.</p>
{% else %}
<p class="muted">From the weekly automated run (chip_strategy=auto, &lambda;=0.5) &middot; computed {{ suggestion_timestamp }} &middot; suggestion only, nothing applied to the live team.</p>
{% if suggestion_is_new_squad %}
<p class="muted">This is the initial squad build (no transfer history yet) - {{ suggested_transfers[1]|length }} players.</p>
{% else %}
{% for gw, moves in suggested_transfers.items()|sort %}
<div class="card">
  <b>GW{{ gw }}</b>
  {% if suggestion_chip.get(gw) %}<span class="tag play">{{ suggestion_chip[gw]|upper }}</span>{% endif %}
  <span class="muted"> &middot; {{ "%+.1f"|format(suggestion_gain) }} pts vs. no transfers</span>
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

<h2>Squad injuries &amp; availability</h2>
{% if not absences %}
<p class="muted">No current injuries/suspensions affecting the squad.</p>
{% else %}
<table>
<tr><th>Player</th><th>Reason</th><th>Details</th><th>Expected back</th></tr>
{% for a in absences %}
<tr><td>{{ a.name }}</td><td>{{ a.reason }}</td><td class="muted">{{ a.details or '-' }}</td><td>{{ a.expected_back }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h2>Upcoming fixtures</h2>
{% if not upcoming_fixtures %}
<p class="muted">No fixture data available.</p>
{% else %}
<table>
<tr><th>Team</th>{% for gw in fixture_gameweeks %}<th>GW{{ gw }}</th>{% endfor %}</tr>
{% for team, gws in upcoming_fixtures.items()|sort %}
<tr><td>{{ team }}</td>
{% for gw in fixture_gameweeks %}
<td>{{ gws.get(gw, ['-'])|join(', ') }}</td>
{% endfor %}
</tr>
{% endfor %}
</table>
{% endif %}

<h2>Price change momentum</h2>
<p class="muted">Net transfers since the last snapshot, ranked - a <b>momentum indicator, not a calibrated rise/fall prediction</b>. Backtesting a simple threshold against real historical data found weak precision (see airsenal/framework/price_change.py's docstring) - treat this as "who's moving", not "who will change price".</p>
{% if price_momentum_error %}
<p class="err">{{ price_momentum_error }}</p>
{% elif not price_movers %}
<p class="muted">No momentum data yet - needs at least two daily snapshots (see tools/price_change_snapshot_run.sh, runs every 4 hours). Check back once that's been running for a day or more.</p>
{% else %}
<table>
<tr><th>Player</th><th>Price</th><th>Net transfers</th><th>Momentum</th><th>Owned by</th><th>Status</th></tr>
{% for p in price_movers %}
<tr><td>{{ p.name }}{% if p.in_squad %} <span class="tag play">SQUAD</span>{% endif %}</td><td>&pound;{{ p.price / 10 }}m</td><td>{{ "%+d"|format(p.net_transfers_today) }}</td><td>{{ "%+.2f"|format(p.momentum_pct) }}%</td><td>{{ p.selected_by_percent }}%</td><td class="muted">{{ p.status }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h2>Replay validation results</h2>
{% if not replay_by_season %}
<p class="muted">No replay results found yet.</p>
{% else %}
<p class="muted">off/greedy/auto chip-strategy comparison from season replays (see docs/chip_timing_spec.md &sect;4.4). "greedy" = chips playable any week; "auto" = the decision-rule recommender at the given &lambda;.</p>
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
<p class="muted" style="font-size:.85em">All runs use reduced-fidelity settings (num_iterations=15, weeks_ahead=2) for tractability - see memory notes for the full methodology and caveats.</p>
{% endif %}

</body>
</html>
"""


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


def _load_upcoming_fixtures(squad_teams, season, next_gw, n_weeks=5):
    """{team: {gameweek: [opponent strings]}} for the given teams over the
    next n_weeks gameweeks."""
    gw_range = list(range(next_gw, next_gw + n_weeks))
    fixtures = session.scalars(
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


def _load_price_movers(season, squad_player_ids, dbsession, top_n=10):
    """Top N risers and fallers by momentum (see get_price_momentum), each
    flagged with whether they're in the current squad."""
    results = get_price_momentum(season=season, dbsession=dbsession)
    if not results:
        return []
    squad_ids = set(squad_player_ids)
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
                "in_squad": r.player_id in squad_ids,
            }
        )
    return sorted(out, key=lambda p: p["momentum_pct"], reverse=True)


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


@app.route("/")
def index():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

    latest_tag = None
    try:
        latest_tag = get_latest_prediction_tag(season=CURRENT_SEASON)
        ops_stats.append(
            {
                "label": "Latest prediction tag",
                "value": latest_tag[:14] + "...",
                "cls": "ok",
            }
        )
    except RuntimeError as e:
        ops_stats.append(
            {
                "label": "Latest prediction tag",
                "value": "none",
                "cls": "warn",
                "note": str(e),
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

    chip_report = None
    chip_error = None
    try:
        # NOTE: build_chip_report reconstructs the squad via the same
        # gameweek < next_gw logic noted below - at the very start of a
        # season (NEXT_GAMEWEEK == 1) that excludes the GW1 squad-selection
        # transactions themselves, so this panel will show a "no squad yet"
        # error for the first gameweek only. It resolves itself automatically
        # once NEXT_GAMEWEEK advances past 1 - not worth overriding
        # build_chip_report's internal gameweek horizon to route around it,
        # since that also disables its live-API squad lookup (it requires
        # next_gw == NEXT_GAMEWEEK to use the API).
        chip_report = build_chip_report(FPL_TEAM_ID, season=CURRENT_SEASON)
    except (ValueError, RuntimeError) as e:
        chip_error = str(e)

    squad_players = []
    squad_error = None
    squad_predicted_total = None
    next_gw = NEXT_GAMEWEEK
    try:
        tag = latest_tag or get_latest_prediction_tag(season=CURRENT_SEASON)
        # get_starting_squad reconstructs from transactions with
        # gameweek < next_gw, so at the very start of a season (NEXT_GAMEWEEK
        # == 1) the GW1 "buy" transactions themselves would be excluded -
        # request gameweek 2 instead so they're included; the squad hasn't
        # changed since, so this is still the right squad to display.
        squad = get_starting_squad(
            next_gw=max(next_gw, 2),
            season=CURRENT_SEASON,
            fpl_team_id=FPL_TEAM_ID,
            use_api=False,
        )
        for p in squad.players:
            preds = get_predicted_points_for_player(
                p.player_id, tag, season=CURRENT_SEASON
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
    except (ValueError, RuntimeError) as e:
        squad_error = str(e)

    suggested_transfers: dict = {}
    suggestion_chip: dict = {}
    suggestion_gain = 0.0
    suggestion_timestamp = None
    suggestion_is_new_squad = False
    try:
        (
            suggested_transfers,
            suggestion_chip,
            suggestion_gain,
            suggestion_timestamp,
            suggestion_is_new_squad,
        ) = _load_suggested_transfers(FPL_TEAM_ID, CURRENT_SEASON, session)
    except (ValueError, RuntimeError) as e:
        suggestion_timestamp = f"error: {e}"

    absences = []
    upcoming_fixtures = {}
    fixture_gameweeks = []
    if squad_players:
        try:
            squad_player_ids = [p.player_id for p in squad.players]
            absences = _load_absences(
                squad_player_ids, CURRENT_SEASON, next_gw, session
            )
        except (ValueError, RuntimeError):
            absences = []
        try:
            squad_teams = {p["team"] for p in squad_players}
            upcoming_fixtures, fixture_gameweeks = _load_upcoming_fixtures(
                squad_teams, CURRENT_SEASON, next_gw
            )
        except (ValueError, RuntimeError):
            upcoming_fixtures, fixture_gameweeks = {}, []
    else:
        squad_player_ids = []

    price_movers = []
    price_momentum_error = None
    try:
        price_movers = _load_price_movers(CURRENT_SEASON, squad_player_ids, session)
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
        ops_stats=ops_stats,
        chip_report=chip_report,
        chip_error=chip_error,
        squad_players=squad_players,
        squad_error=squad_error,
        squad_predicted_total=squad_predicted_total,
        next_gw=next_gw,
        replay_by_season=replay_by_season,
        replay_combined=replay_combined,
        replay_configs=replay_configs,
        best_combined_config=best_combined_config,
        suggested_transfers=suggested_transfers,
        suggestion_chip=suggestion_chip,
        suggestion_gain=suggestion_gain,
        suggestion_timestamp=suggestion_timestamp,
        suggestion_is_new_squad=suggestion_is_new_squad,
        absences=absences,
        upcoming_fixtures=upcoming_fixtures,
        fixture_gameweeks=fixture_gameweeks,
        price_movers=price_movers,
        price_momentum_error=price_momentum_error,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("DASHBOARD_PORT", "5050")),
        debug=False,
    )
