"""
Chip timing & opportunity-cost model.

This module figures out *when* each FPL chip (wildcard, free hit, bench boost,
triple captain) can be played, and (in later PRs) how much it is worth playing
in a given gameweek versus holding it for a better future gameweek. See
``docs/chip_timing_spec.md`` for the full design.

This first module only provides the foundational pieces: the shared
dataclasses, and the two functions needed to know *when a chip's window is*
and *what double/blank gameweeks look like* - no value estimation or
decision-making yet, and no change to existing optimiser behaviour.
"""

import warnings
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm.session import Session

from airsenal.framework.data_fetcher import FPLDataFetcher
from airsenal.framework.schema import Transaction, TransferSuggestion, session
from airsenal.framework.utils import (
    CURRENT_SEASON,
    fetcher,
    get_fixture_teams,
    get_fixtures_for_gameweek,
    get_max_gameweek,
    list_teams,
)

# Map from the FPL API's chip names (as found in bootstrap-static's "chips"
# key, and in the per-entry "chips" list returned by the my-team endpoint) to
# the names AIrsenal uses internally.
API_CHIP_NAME_MAP: dict[str, str] = {
    "wildcard": "wildcard",
    "freehit": "free_hit",
    "bboost": "bench_boost",
    "3xc": "triple_captain",
}


@dataclass
class ChipWindow:
    name: str  # "wildcard" | "free_hit" | "bench_boost" | "triple_captain"
    start_gw: int  # first GW the chip can be played
    end_gw: int  # last GW the chip can be played (deadline)
    available: bool  # False if already played / not owned


@dataclass
class ChipValue:
    chip: str
    gameweek: int
    expected_gain: float  # estimated points gained by playing chip that GW
    method: str  # "prediction" (in-horizon) or "proxy" (long-range)
    is_double: bool  # whether the relevant team(s) have a DGW
    notes: str = ""


@dataclass
class ChipRecommendation:
    chip: str
    play_now: bool  # play in the next gameweek?
    best_gw: int  # argmax of (discounted) expected gain
    gain_now: float
    best_future_gain: float  # uncertainty-discounted best gain after next GW
    deadline_gw: int
    values: list[ChipValue] = field(default_factory=list)  # full per-GW table


def _fallback_chip_windows(season: str) -> list[ChipWindow]:
    """Hardcoded chip windows, used when the bootstrap-static API is
    unavailable, or when looking at a season other than the current one (the
    live API only ever reflects the current season).

    2025/26 onwards: two windows per chip, one per half of the season -
    transfer chips (wildcard, free_hit) GW2-19 / GW20-38, team chips
    (bench_boost, triple_captain) GW1-19 / GW20-38 - matching the 2025/26
    rule change to two-of-each-chip-per-season.

    Before 2025/26: only wildcard was split into two halves (GW2-19,
    GW20-38, as it has been since the "second wildcard" rule was introduced);
    free_hit/bench_boost/triple_captain each had a single window covering the
    whole season (free_hit GW2-38, bench_boost/triple_captain GW1-38),
    matching FPL's historic (pre-2025/26) one-chip-per-season rule for those
    three chips.
    """
    if season >= "2526":
        return [
            ChipWindow("wildcard", 2, 19, True),
            ChipWindow("wildcard", 20, 38, True),
            ChipWindow("free_hit", 2, 19, True),
            ChipWindow("free_hit", 20, 38, True),
            ChipWindow("bench_boost", 1, 19, True),
            ChipWindow("bench_boost", 20, 38, True),
            ChipWindow("triple_captain", 1, 19, True),
            ChipWindow("triple_captain", 20, 38, True),
        ]
    return [
        ChipWindow("wildcard", 2, 19, True),
        ChipWindow("wildcard", 20, 38, True),
        ChipWindow("free_hit", 2, 38, True),
        ChipWindow("bench_boost", 1, 38, True),
        ChipWindow("triple_captain", 1, 38, True),
    ]


def _infer_availability_from_db(
    windows: list[ChipWindow], season: str, fpl_team_id: int | None
) -> None:
    """Mark a window unavailable if the database shows the chip was already
    played within that window's gameweek range.

    Uses ``TransferSuggestion.chip_played`` (the chip recorded against a
    gameweek by a previous optimisation run - the values used there are
    exactly the AIrsenal chip names: "wildcard", "free_hit", "bench_boost",
    "triple_captain", see ``optimization_utils.py``) as the primary source,
    plus ``Transaction.free_hit`` as a secondary source specifically for
    free_hit (in case transactions were recorded without a corresponding
    TransferSuggestion row, e.g. older/manually-applied data).
    """
    played_gws: dict[str, set[int]] = defaultdict(set)

    ts_query = select(
        TransferSuggestion.chip_played, TransferSuggestion.gameweek
    ).where(
        TransferSuggestion.season == season,
        TransferSuggestion.chip_played.is_not(None),
    )
    if fpl_team_id is not None:
        ts_query = ts_query.where(TransferSuggestion.fpl_team_id == fpl_team_id)
    for chip_played, gameweek in session.execute(ts_query).all():
        played_gws[chip_played].add(gameweek)

    fh_query = select(Transaction.gameweek).where(
        Transaction.season == season, Transaction.free_hit == 1
    )
    if fpl_team_id is not None:
        fh_query = fh_query.where(Transaction.fpl_team_id == fpl_team_id)
    played_gws["free_hit"].update(session.scalars(fh_query).all())

    for window in windows:
        if any(
            window.start_gw <= gw <= window.end_gw
            for gw in played_gws.get(window.name, ())
        ):
            window.available = False


def get_chip_windows(
    fpl_team_id: int | None = None,
    season: str = CURRENT_SEASON,
    apifetcher: FPLDataFetcher = fetcher,
    use_api: bool = True,
) -> list[ChipWindow]:
    """Chip windows + availability.

    - Windows from the public bootstrap-static "chips" key (no auth needed);
      map API names (freehit/bboost/3xc) to AIrsenal names.
    - Availability from apifetcher.get_available_chips() when logged in. Note
      this is applied per chip *name*, not per individual window - if a chip
      name has two windows (one per half of the season) the API's "available"
      flag for that name is applied to both, since the live API does not
      currently distinguish between them for this purpose.
    - Fallbacks (API unavailable, or replay of a past season): hardcoded
      windows (GW2-19 / GW20-38 for transfer chips; GW1-19 / GW20-38 for team
      chips; for seasons before 2526, a single window GW1/2-38 per chip with
      the extra wildcard at GW20+), and availability inferred from the
      TransferSuggestion/Transaction tables (chip_played / free_hit columns).
    """
    windows: list[ChipWindow] | None = None

    if use_api and season == CURRENT_SEASON:
        try:
            chips_data = apifetcher.get_current_summary_data()["chips"]
            api_windows = [
                ChipWindow(
                    name=API_CHIP_NAME_MAP[chip["name"]],
                    start_gw=chip["start_event"],
                    end_gw=chip["stop_event"],
                    available=True,
                )
                for chip in chips_data
                if chip["name"] in API_CHIP_NAME_MAP
            ]
            if api_windows:
                windows = api_windows
        except Exception as e:
            warnings.warn(
                f"Failed to get chip windows from the FPL API:\n{e}\nFalling back to "
                "hardcoded chip windows.",
                stacklevel=2,
            )

    if windows is None:
        windows = _fallback_chip_windows(season)

    if use_api and season == CURRENT_SEASON:
        try:
            available_names = {
                API_CHIP_NAME_MAP[name]
                for name in apifetcher.get_available_chips(fpl_team_id)
                if name in API_CHIP_NAME_MAP
            }
            for window in windows:
                window.available = window.name in available_names
            return windows
        except Exception as e:
            warnings.warn(
                f"Failed to get available chips from the FPL API:\n{e}\nFalling back "
                "to inferring chip availability from the database.",
                stacklevel=2,
            )

    _infer_availability_from_db(windows, season, fpl_team_id)
    return windows


def get_double_blank_gameweeks(
    season: str = CURRENT_SEASON, dbsession: Session = session
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Detect double and blank gameweeks from the Fixture table.

    Count fixtures per (team, gameweek) over all 20 teams (Fixture has
    home_team, away_team, gameweek, season - see schema.py:293; use
    get_fixture_teams / get_fixtures_for_gameweek in framework/utils.py).
    Returns ({gw: [teams with 2+ fixtures]}, {gw: [teams with 0 fixtures]}).
    NOTE: late-season fixture rearrangements mean future GWs can change; the
    report should recompute on every run (no caching across DB updates).
    """
    teams = [t["name"] for t in list_teams(season, dbsession)]
    max_gw = get_max_gameweek(season, dbsession)

    doubles: dict[int, list[str]] = {}
    blanks: dict[int, list[str]] = {}

    for gw in range(1, max_gw + 1):
        # Fixture.gameweek is nullable for fixtures that haven't been
        # scheduled yet; get_fixtures_for_gameweek(gw, ...) only ever returns
        # fixtures with gameweek == gw, so unscheduled fixtures (gameweek is
        # None) are naturally excluded from this per-gameweek count.
        fixtures = get_fixtures_for_gameweek(gw, season, dbsession)

        team_counts: dict[str, int] = defaultdict(int)
        for home, away in get_fixture_teams(fixtures):
            team_counts[home] += 1
            team_counts[away] += 1

        gw_doubles = [team for team in teams if team_counts.get(team, 0) >= 2]
        gw_blanks = [team for team in teams if team_counts.get(team, 0) == 0]

        if gw_doubles:
            doubles[gw] = gw_doubles
        if gw_blanks:
            blanks[gw] = gw_blanks

    return doubles, blanks
