"""
airsenal_chip_report: report estimated per-gameweek chip value and a
play-now-or-hold recommendation for each available chip (wildcard, free
hit, bench boost, triple captain).

See docs/chip_timing_spec.md §4.2. This is a read-only report - it does not
change any existing optimiser behaviour, and (per this PR's scope, see
airsenal/framework/chip_timing.py's module docstring) is not yet wired into
airsenal_run_optimization/airsenal_run_pipeline.
"""

import argparse
import contextlib
import io
import json
import sys

from airsenal.framework.chip_timing import (
    ChipRecommendation,
    ChipValue,
    ChipWindow,
    get_chip_windows,
    get_double_blank_gameweeks,
    recommend_chip_timing,
)
from airsenal.framework.optimization_utils import get_starting_squad
from airsenal.framework.schema import session
from airsenal.framework.squad import Squad
from airsenal.framework.utils import (
    CURRENT_SEASON,
    fetcher,
    get_gameweeks_array,
    get_latest_prediction_tag,
    list_teams,
)


def _team_full_names(season: str, dbsession=session) -> dict[str, str]:
    return {t["name"]: t["full_name"] for t in list_teams(season, dbsession)}


def build_chip_report(
    fpl_team_id: int,
    season: str = CURRENT_SEASON,
    tag: str | None = None,
    weeks_ahead: int = 3,
    risk_lambda: float = 0.8,
    dbsession=session,
    gameweeks: list[int] | None = None,
) -> dict:
    """Build a machine-readable chip report: per-GW values and a
    recommendation for every chip that's currently available, plus a note
    for chips that aren't (already played, not owned, or window not yet
    open).

    ``gameweeks``, if given, overrides the horizon computed from
    ``weeks_ahead`` (mainly useful for tests - get_gameweeks_array requires
    an explicit gameweek_start for non-current seasons, which this CLI
    doesn't otherwise expose).

    Raises ValueError if there's no existing squad to report on (e.g. a
    brand new team that hasn't been given a starting squad yet - matching
    docs/chip_timing_spec.md §5's "season start" edge case, which says the
    chip recommendation should be skipped entirely in that situation).
    """
    if not tag:
        tag = get_latest_prediction_tag(season=season)

    if gameweeks is None:
        gameweeks = get_gameweeks_array(
            weeks_ahead=weeks_ahead, season=season, dbsession=dbsession
        )
    next_gw = gameweeks[0]

    try:
        squad: Squad = get_starting_squad(
            next_gw=next_gw,
            season=season,
            fpl_team_id=fpl_team_id,
            use_api=(season == CURRENT_SEASON),
            apifetcher=fetcher,
        )
    except (ValueError, TypeError) as e:
        msg = (
            f"No existing squad found for fpl_team_id {fpl_team_id} in "
            f"{season} - can't report on chip timing for a squad that "
            f"doesn't exist yet (e.g. before the season has started for "
            f"this team)."
        )
        raise ValueError(msg) from e

    chip_windows = get_chip_windows(
        fpl_team_id=fpl_team_id,
        season=season,
        apifetcher=fetcher,
        use_api=(season == CURRENT_SEASON),
    )
    recommendations = recommend_chip_timing(
        chip_windows,
        squad,
        tag,
        gameweeks,
        season=season,
        risk_lambda=risk_lambda,
        dbsession=dbsession,
    )
    recs_by_chip = {rec.chip: rec for rec in recommendations}
    doubles, blanks = get_double_blank_gameweeks(season, dbsession)
    full_names = _team_full_names(season, dbsession)

    windows_by_chip: dict[str, list[ChipWindow]] = {}
    for window in chip_windows:
        windows_by_chip.setdefault(window.name, []).append(window)

    chips_report: dict[str, dict] = {}
    for chip_name, windows in windows_by_chip.items():
        # the window recommend_chip_timing actually used (if any) is the
        # one with a recommendation; otherwise report the earliest
        # available-or-not window for visibility.
        rec = recs_by_chip.get(chip_name)
        window = next(
            (w for w in windows if rec and w.start_gw <= rec.best_gw <= w.end_gw),
            min(windows, key=lambda w: w.start_gw),
        )
        chips_report[chip_name] = {
            "window": {
                "start_gw": window.start_gw,
                "end_gw": window.end_gw,
                "available": window.available,
            },
            "values": [
                _value_to_dict(v, doubles, blanks, full_names) for v in rec.values
            ]
            if rec
            else [],
            "recommendation": _recommendation_to_dict(rec) if rec else None,
        }

    return {
        "season": season,
        "tag": tag,
        "next_gw": next_gw,
        "weeks_ahead": weeks_ahead,
        "risk_lambda": risk_lambda,
        "fpl_team_id": fpl_team_id,
        "chips": chips_report,
    }


def _value_to_dict(
    value: ChipValue,
    doubles: dict[int, list[str]],
    blanks: dict[int, list[str]],
    full_names: dict[str, str],
) -> dict:
    double_teams = [full_names.get(t, t) for t in doubles.get(value.gameweek, [])]
    blank_teams = [full_names.get(t, t) for t in blanks.get(value.gameweek, [])]
    return {
        "gameweek": value.gameweek,
        "expected_gain": round(value.expected_gain, 2),
        "method": value.method,
        "is_double": value.is_double,
        "notes": value.notes,
        "double_teams": double_teams,
        "blank_teams": blank_teams,
    }


def _recommendation_to_dict(rec: ChipRecommendation) -> dict:
    return {
        "play_now": rec.play_now,
        "best_gw": rec.best_gw,
        "gain_now": round(rec.gain_now, 2),
        "best_future_gain": round(rec.best_future_gain, 2),
        "deadline_gw": rec.deadline_gw,
    }


def format_report_text(report: dict) -> str:
    """Render a build_chip_report() dict as the human-readable text format
    shown in docs/chip_timing_spec.md §4.2."""
    lines = [
        f"Chip timing report - {report['season']}, next GW{report['next_gw']} "
        f"(tag={report['tag']}, risk_lambda={report['risk_lambda']})",
        "",
    ]
    for chip_name in ("wildcard", "free_hit", "bench_boost", "triple_captain"):
        chip = report["chips"].get(chip_name)
        if chip is None:
            continue
        window = chip["window"]
        title = chip_name.replace("_", " ").upper()
        header = f"{title} (window GW{window['start_gw']}-{window['end_gw']}"
        header += ", available)" if window["available"] else ", not available)"
        lines.append(header)

        if not window["available"]:
            lines.append("  (already played, or not owned this half-season)")
            lines.append("")
            continue

        for value in chip["values"]:
            markers = []
            if value["double_teams"]:
                markers.append("DOUBLE GAMEWEEK: " + ", ".join(value["double_teams"]))
            if value["blank_teams"]:
                markers.append("BLANK GAMEWEEK: " + ", ".join(value["blank_teams"]))
            marker_str = f", {'; '.join(markers)}" if markers else ""
            lines.append(
                f"  GW{value['gameweek']}: {value['expected_gain']:+.1f} "
                f"({value['method']}{marker_str})"
            )

        rec = chip["recommendation"]
        if rec is not None:
            if rec["play_now"]:
                lines.append(
                    f"  RECOMMENDATION: PLAY in GW{rec['best_gw']} "
                    f"({rec['gain_now']:+.1f} now vs "
                    f"{rec['best_future_gain']:+.1f} best future)"
                )
            else:
                lines.append(
                    f"  RECOMMENDATION: HOLD - best estimated gameweek "
                    f"GW{rec['best_gw']} ({rec['best_future_gain']:+.1f} vs "
                    f"{rec['gain_now']:+.1f} now)"
                )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Report estimated per-gameweek chip value and a play-now-or-hold "
            "recommendation for each available chip."
        )
    )
    parser.add_argument(
        "--fpl_team_id", help="specify fpl team id", type=int, required=False
    )
    parser.add_argument(
        "--season",
        help="what season, in format e.g. '2021'",
        type=str,
        default=CURRENT_SEASON,
    )
    parser.add_argument(
        "--tag",
        help="prediction tag to use (default: latest)",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--weeks_ahead",
        help="how many weeks ahead count as 'in-horizon'",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--risk_lambda",
        help="risk-aversion parameter for the play-now-vs-hold decision",
        type=float,
        default=0.8,
    )
    parser.add_argument(
        "--json", help="output machine-readable JSON", action="store_true"
    )
    args = parser.parse_args()

    fpl_team_id = args.fpl_team_id or fetcher.FPL_TEAM_ID
    if fpl_team_id is None:
        print(
            "ERROR: fpl_team_id must be specified as --fpl_team_id, or via the "
            "FPL_TEAM_ID environment variable/config."
        )
        sys.exit(1)

    # build_chip_report (via get_starting_squad/get_chip_windows) prints
    # informational messages as a side effect; keep --json output pure JSON
    # on stdout by capturing those while building the report.
    build_error: ValueError | None = None
    stdout_buffer = io.StringIO() if args.json else None
    with (
        contextlib.redirect_stdout(stdout_buffer)
        if args.json
        else contextlib.nullcontext()
    ):
        try:
            report = build_chip_report(
                fpl_team_id=fpl_team_id,
                season=args.season,
                tag=args.tag,
                weeks_ahead=args.weeks_ahead,
                risk_lambda=args.risk_lambda,
            )
        except ValueError as e:
            build_error = e

    if build_error is not None:
        print(f"ERROR: {build_error}")
        sys.exit(1)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report_text(report))


if __name__ == "__main__":
    main()
