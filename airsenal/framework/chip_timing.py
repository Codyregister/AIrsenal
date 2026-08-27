"""
Chip timing & opportunity-cost model.

This module figures out *when* each FPL chip (wildcard, free hit, bench boost,
triple captain) can be played, and how much it is worth playing in a given
gameweek versus holding it for a better future gameweek. See
``docs/chip_timing_spec.md`` for the full design.

Beyond the PR1 foundations (the shared dataclasses, ``get_chip_windows``, and
``get_double_blank_gameweeks``), this module provides:

- Four per-chip value estimators (``estimate_triple_captain_value``,
  ``estimate_bench_boost_value``, ``estimate_wildcard_value``,
  ``estimate_free_hit_value``), each with a cheap in-horizon "prediction"
  mode (used when the DB has real points predictions for the target
  gameweek) and a cheap long-range "proxy" mode (used otherwise, relying on
  the fitted team model and fixture-count heuristics rather than running the
  full player-prediction/GA-optimiser machinery for every remaining
  gameweek of the season).
- ``recommend_chip_timing``, the decision rule that turns those per-GW
  values into a play-now-or-hold recommendation for each available chip.

``recommend_chip_timing`` is wired into ``run_optimization``/the pipeline
behind ``--chip_strategy auto`` (default ``off``, i.e. no behaviour change
unless explicitly requested) - see
``airsenal/scripts/fill_transfersuggestion_table.py``'s
``resolve_auto_chip_gameweeks`` and ``docs/chip_timing_spec.md`` §4.3/§8
PR3.
"""

import warnings
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm.session import Session

from airsenal.framework.bpl_interface import (
    fixture_probabilities,
    get_fitted_team_model,
)
from airsenal.framework.data_fetcher import FPLDataFetcher
from airsenal.framework.optimization_squad import make_new_squad
from airsenal.framework.optimization_utils import check_tag_valid, get_discount_factor
from airsenal.framework.schema import Transaction, TransferSuggestion, session
from airsenal.framework.squad import Squad
from airsenal.framework.utils import (
    CURRENT_SEASON,
    fetcher,
    get_fixture_teams,
    get_fixtures_for_gameweek,
    get_last_complete_gameweek_in_db,
    get_max_gameweek,
    get_player,
    get_predicted_points_for_player,
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


# --------------------------------------------------------------------------
# Per-chip value estimators (docs/chip_timing_spec.md §4.1)
# --------------------------------------------------------------------------
#
# Each estimator has two regimes, selected by whether the DB has real points
# predictions for the target gameweek (check_tag_valid):
#
# - "prediction" (in-horizon): uses the real prediction machinery
#   (Squad.get_expected_points, get_predicted_points_for_player, or - for
#   wildcard/free hit - a full make_new_squad GA optimisation). Only ever
#   run for the handful of gameweeks that have predictions (typically the
#   next few), since the GA-based estimates in particular are expensive.
# - "proxy" (long-range): cheap heuristics based on the fitted team model
#   (win/draw probability) and fixture counts (get_double_blank_gameweeks),
#   used for every other remaining gameweek of the season. These must stay
#   well under 1s per gameweek, which is why the team model is fit once and
#   cached (see _get_cached_team_model) rather than refit per call.
#
# The constants below are coarse, documented heuristics for the proxy
# regime - precision matters far less here than for in-horizon estimates,
# since a gameweek many weeks away is inherently uncertain (see
# docs/chip_timing_spec.md §4.1, wildcard docstring).

PREMIUM_CAPTAIN_BASELINE_PTS = 7.0  # ~pts/fixture for a generic top captain pick
BENCH_BASELINE_TOTAL_PTS = 7.0  # ~total pts for a full bench in a normal (non-DGW) GW
AVG_STARTER_PTS = 4.0  # ~pts for an average replacement starter (free hit blanks)
DGW_UPSIDE_PTS_PER_TEAM = 3.0  # extra value per doubling team a free-hit XI can exploit
STALENESS_PTS_PER_ISSUE = (
    2.0  # ~pts cost per "stale squad" issue, see estimate_wildcard_value
)
PROXY_UNCERTAINTY_PENALTY = 0.9  # discount applied to proxy-derived future values
WILDCARD_PROXY_HORIZON = (
    3  # GWs used for the in-horizon wildcard optimum-squad comparison
)

# Keyed on the *database* a session talks to, never on id(dbsession): CPython
# recycles object addresses after garbage collection, so a short-lived session
# (e.g. one created per web request) can land on a freed address and collide
# with an unrelated entry. With two teams whose separate databases share a
# season, that served one team a model fitted against the other team's data.
_team_model_cache: dict[tuple[str, str, int | None], object] = {}
_captain_baseline_cache: dict[
    tuple[str, str, str, tuple[int, ...]], tuple[str | None, float]
] = {}


def _dbsession_cache_key(dbsession: Session) -> str | None:
    """A stable identifier for the database behind a session, or None if it
    can't be determined.

    Callers must skip caching entirely on None rather than falling back to
    id(dbsession) - see the note on _team_model_cache above.
    """
    try:
        bind = dbsession.get_bind()
    except Exception:
        return None
    url = getattr(bind, "url", None)
    if url is None:
        return None
    text = str(url)
    # Every in-memory SQLite database renders as the same URL despite being a
    # genuinely separate database, so it can't identify one. Decline to cache
    # rather than risk conflating two of them.
    if ":memory:" in text or "mode=memory" in text:
        return None
    return text


def _get_cached_team_model(season: str, dbsession: Session):
    """Fit and cache the team model used by the long-range proxies below.

    Fitting a Dixon-Coles-style team model is the expensive part of using
    it - evaluating fixture probabilities from an already-fitted model is
    cheap. Caching here is what keeps the proxy estimators within the
    <1s-per-GW budget required by the spec: without it, a proxy call for
    every remaining gameweek of the season would each refit the model from
    scratch. get_fitted_team_model only ever trains on completed Result
    rows regardless of the "gameweek" cutoff passed to it, so it doesn't
    matter which gameweek we happen to fit with first for a given
    season/database.

    The last complete gameweek is part of the key so that a long-lived
    process (e.g. the dashboard service) refits once new results land,
    rather than serving a model fitted before the latest gameweek was
    played for the rest of the season.
    """
    db_key = _dbsession_cache_key(dbsession)
    if db_key is None:
        return get_fitted_team_model(
            season, get_max_gameweek(season, dbsession), dbsession
        )
    key = (season, db_key, get_last_complete_gameweek_in_db(season, dbsession))
    if key not in _team_model_cache:
        max_gw = get_max_gameweek(season, dbsession)
        model = get_fitted_team_model(season, max_gw, dbsession)
        # Only the newest fit for a season/database is ever asked for, so drop
        # superseded ones instead of retaining a fitted model per gameweek.
        for stale in [k for k in _team_model_cache if k[:2] == key[:2]]:
            del _team_model_cache[stale]
        _team_model_cache[key] = model
    return _team_model_cache[key]


def _team_fixture_strength(
    team: str, gw: int, season: str, dbsession: Session
) -> float:
    """A team-model-derived scaling factor in [0, 1] for how favourable a
    team's fixture(s) are in a gameweek: win probability plus half the draw
    probability, averaged over the team's fixture(s) that GW (so doubles
    naturally average rather than double-count).

    Returns a neutral 0.5 if the team has no fixture that GW, or if fixture
    probabilities aren't available for any reason (e.g. the team model
    failed to fit) - this is a proxy signal, not a critical calculation, so
    fail soft rather than raise.
    """
    try:
        model = _get_cached_team_model(season, dbsession)
        probs = fixture_probabilities(gw, season, model=model, dbsession=dbsession)
    except Exception:
        return 0.5
    scales = [
        row.home_win_probability + 0.5 * row.draw_probability
        for row in probs.itertuples()
        if row.home_team == team
    ] + [
        row.away_win_probability + 0.5 * row.draw_probability
        for row in probs.itertuples()
        if row.away_team == team
    ]
    if not scales:
        return 0.5
    return sum(scales) / len(scales)


def _best_captain_baseline(
    squad: Squad, tag: str, season: str, dbsession: Session
) -> tuple[str | None, float]:
    """Best current-squad captain candidate for the long-range triple
    captain proxy: whichever squad player has the highest average
    points-per-fixture across any gameweeks predictions exist for, and
    that average. Falls back to (None, PREMIUM_CAPTAIN_BASELINE_PTS) if the
    squad has no predictions at all (e.g. a brand new squad/season).

    Cached per (database, tag, season, squad composition) since it doesn't
    depend on gw - without caching, this would be recomputed (with a handful
    of DB queries each) for every long-range gameweek evaluated. The squad is
    identified by its player ids rather than id(squad), which is unsafe as a
    cache key (see the note on _team_model_cache); player ids are database
    local, hence keying on the database too.
    """
    db_key = _dbsession_cache_key(dbsession)
    key = (
        None
        if db_key is None
        else (db_key, tag, season, tuple(sorted(p.player_id for p in squad.players)))
    )
    if key is not None and key in _captain_baseline_cache:
        return _captain_baseline_cache[key]

    best_team: str | None = None
    best_avg = 0.0
    for p in squad.players:
        preds = get_predicted_points_for_player(p.player_id, tag, season, dbsession)
        nonzero = [v for v in preds.values() if v > 0]
        if not nonzero:
            continue
        avg = sum(nonzero) / len(nonzero)
        if avg > best_avg:
            best_avg = avg
            best_team = p.team

    result = (
        (None, PREMIUM_CAPTAIN_BASELINE_PTS)
        if best_team is None
        else (best_team, best_avg)
    )
    if key is not None:
        _captain_baseline_cache[key] = result
    return result


def estimate_triple_captain_value(
    gw: int,
    squad: Squad,
    tag: str,
    season: str = CURRENT_SEASON,
    dbsession: Session = session,
) -> ChipValue:
    """Estimate the value of playing triple captain in gameweek ``gw``.

    In-horizon (predictions exist for ``gw``): the extra 1x of points the
    chip adds is the best candidate captain's predicted points that
    gameweek - the max, over the current squad, of
    get_predicted_points_for_player (which already sums over the GW's
    fixtures, so doubles are handled automatically).

    Long-range proxy (no predictions yet): (the current squad's best
    captain candidate's average points-per-fixture, or
    PREMIUM_CAPTAIN_BASELINE_PTS if the squad has no predictions at all) x
    (number of fixtures that player's team has in ``gw``, from
    get_double_blank_gameweeks) x (a team-model-derived win/draw scaling
    factor, see _team_fixture_strength).
    """
    doubles, blanks = get_double_blank_gameweeks(season, dbsession)
    double_teams = set(doubles.get(gw, []))
    blank_teams = set(blanks.get(gw, []))

    if check_tag_valid(tag, [gw], season, dbsession):
        best_player = None
        best_points = 0.0
        for p in squad.players:
            points = get_predicted_points_for_player(
                p.player_id, tag, season, dbsession
            ).get(gw, 0.0)
            if points > best_points:
                best_points = points
                best_player = p
        is_double = best_player is not None and best_player.team in double_teams
        notes = f"best candidate: {best_player}" if best_player else "no candidate"
        return ChipValue(
            chip="triple_captain",
            gameweek=gw,
            expected_gain=best_points,
            method="prediction",
            is_double=is_double,
            notes=notes,
        )

    team, points_per_fixture = _best_captain_baseline(squad, tag, season, dbsession)
    if team is None:
        fixture_count = 1
        strength = 0.5
        notes = "no squad predictions available; generic premium-player baseline"
        is_double = False
    else:
        if team in blank_teams:
            fixture_count = 0
        elif team in double_teams:
            fixture_count = 2
        else:
            fixture_count = 1
        strength = _team_fixture_strength(team, gw, season, dbsession)
        notes = f"proxy based on best captain candidate ({team})"
        is_double = team in double_teams
    return ChipValue(
        chip="triple_captain",
        gameweek=gw,
        expected_gain=points_per_fixture * fixture_count * strength,
        method="proxy",
        is_double=is_double,
        notes=notes,
    )


def estimate_bench_boost_value(
    gw: int,
    squad: Squad,
    tag: str,
    season: str = CURRENT_SEASON,
    dbsession: Session = session,
) -> ChipValue:
    """Estimate the value of playing bench boost in gameweek ``gw``.

    In-horizon: squad.get_expected_points(gw, tag, bench_boost=True) minus
    squad.get_expected_points(gw, tag) - the bench's contribution with full
    (bench-boost) weights, via Squad.total_points_for_subs.

    Long-range proxy: the current bench (whichever 4 players aren't
    currently flagged as starting XI, reflecting the most recently
    optimised lineup - a future gameweek's actual bench composition isn't
    knowable this far out) x a baseline points-per-fixture derived from
    BENCH_BASELINE_TOTAL_PTS (split evenly across the 4 bench slots),
    scaled by each bench player's fixture count that GW (so doubles count
    double, blanks count zero).
    """
    doubles, blanks = get_double_blank_gameweeks(season, dbsession)
    double_teams = set(doubles.get(gw, []))
    blank_teams = set(blanks.get(gw, []))

    if check_tag_valid(tag, [gw], season, dbsession):
        with_bb = squad.get_expected_points(gw, tag, bench_boost=True)
        without_bb = squad.get_expected_points(gw, tag)
        bench = [p for p in squad.players if not p.is_starting]
        is_double = any(p.team in double_teams for p in bench)
        return ChipValue(
            chip="bench_boost",
            gameweek=gw,
            expected_gain=with_bb - without_bb,
            method="prediction",
            is_double=is_double,
        )

    bench = [p for p in squad.players if not p.is_starting]
    if not bench:
        # lineup never optimised (e.g. brand new squad) - approximate the
        # bench as the 4 lowest-priced players.
        bench = sorted(squad.players, key=lambda p: p.purchase_price)[:4]
    per_slot = BENCH_BASELINE_TOTAL_PTS / 4
    gain = 0.0
    is_double = False
    for p in bench:
        if p.team in blank_teams:
            fixture_count = 0
        elif p.team in double_teams:
            fixture_count = 2
            is_double = True
        else:
            fixture_count = 1
        gain += per_slot * fixture_count
    return ChipValue(
        chip="bench_boost",
        gameweek=gw,
        expected_gain=gain,
        method="proxy",
        is_double=is_double,
        notes=f"proxy over {len(bench)} bench players",
    )


def estimate_wildcard_value(
    gw: int,
    squad: Squad,
    tag: str,
    season: str = CURRENT_SEASON,
    dbsession: Session = session,
    num_iterations: int = 100,
) -> ChipValue:
    """Estimate the value of playing wildcard in gameweek ``gw``.

    In-horizon: build an unconstrained optimal squad (via make_new_squad,
    reusing the current squad's total sale value - Squad.sale_value - as
    the budget) for up to WILDCARD_PROXY_HORIZON gameweeks starting at
    ``gw`` (clipped to whichever of those actually have predictions), and
    compare its total expected points over that range to the current
    squad's. This is expensive (runs the GA with num_iterations
    population/generations), so it is only ever done for in-horizon
    gameweeks. Note the estimator only receives a single target gameweek
    rather than a full horizon, so WILDCARD_PROXY_HORIZON is a documented
    simplification of the spec's "[gw, horizon]" wording.

    Long-range proxy: a simple "squad staleness" heuristic - count, among
    the current 15 players: (a) those with no fixture that GW (blank), (b)
    those flagged injured/suspended (<=50% chance of playing, via
    Player.is_injured_or_suspended), and (c) those with a below-average
    upcoming fixture (team-model win/draw scaling < 0.4, see
    _team_fixture_strength) that GW - then multiply the total count by
    STALENESS_PTS_PER_ISSUE. This deliberately doesn't try to estimate the
    *upside* of a full squad rebuild that far out (per the spec, wildcard
    value is dominated by squad state, which is only reliably known
    near-term); it is a coarse relative-ordering signal, not a precise
    points estimate.
    """
    doubles, blanks = get_double_blank_gameweeks(season, dbsession)
    double_teams = set(doubles.get(gw, []))
    blank_teams = set(blanks.get(gw, []))

    if check_tag_valid(tag, [gw], season, dbsession):
        max_gw = get_max_gameweek(season, dbsession)
        gw_range = [
            g
            for g in range(gw, min(gw + WILDCARD_PROXY_HORIZON, max_gw + 1))
            if check_tag_valid(tag, [g], season, dbsession)
        ] or [gw]
        budget = squad.sale_value(gw, use_api=False)
        optimal_squad = make_new_squad(
            gw_range,
            tag,
            budget=budget,
            season=season,
            verbose=False,
            population_size=num_iterations,
            generations=num_iterations,
        )
        optimal_points = sum(
            optimal_squad.get_expected_points(g, tag) for g in gw_range
        )
        current_points = sum(squad.get_expected_points(g, tag) for g in gw_range)
        is_double = any(p.team in double_teams for p in squad.players) or any(
            p.team in double_teams for p in optimal_squad.players
        )
        return ChipValue(
            chip="wildcard",
            gameweek=gw,
            expected_gain=optimal_points - current_points,
            method="prediction",
            is_double=is_double,
            notes=f"optimum vs current squad over GWs {gw_range}",
        )

    n_blank = sum(1 for p in squad.players if p.team in blank_teams)
    n_injured = 0
    for p in squad.players:
        player_row = get_player(p.player_id, dbsession)
        if player_row is not None and player_row.is_injured_or_suspended(
            season, gw, gw
        ):
            n_injured += 1
    n_poor_fixture = sum(
        1
        for p in squad.players
        if p.team not in blank_teams
        and _team_fixture_strength(p.team, gw, season, dbsession) < 0.4
    )
    n_issues = n_blank + n_injured + n_poor_fixture
    is_double = any(p.team in double_teams for p in squad.players)
    return ChipValue(
        chip="wildcard",
        gameweek=gw,
        expected_gain=n_issues * STALENESS_PTS_PER_ISSUE,
        method="proxy",
        is_double=is_double,
        notes=(
            f"staleness proxy: {n_blank} blank, {n_injured} injured/suspended, "
            f"{n_poor_fixture} poor fixture"
        ),
    )


def estimate_free_hit_value(
    gw: int,
    squad: Squad,
    tag: str,
    season: str = CURRENT_SEASON,
    dbsession: Session = session,
    num_iterations: int = 100,
) -> ChipValue:
    """Estimate the value of playing free hit in gameweek ``gw``.

    In-horizon: like wildcard, but for ``gw`` alone (the free hit squad
    reverts after one week) - an unconstrained optimal one-week squad (via
    make_new_squad, reusing the current squad's total sale value as the
    budget) minus the current squad's expected points, both for ``gw``
    only.

    Long-range proxy: dominated by blanks/doubles, per the spec - (number
    of current-squad players with no fixture that GW) x AVG_STARTER_PTS,
    plus a flat DGW upside per doubling team (DGW_UPSIDE_PTS_PER_TEAM)
    reflecting that a free-hit XI could be packed with doubling-team
    players the current squad doesn't have.
    """
    doubles, blanks = get_double_blank_gameweeks(season, dbsession)
    double_teams = set(doubles.get(gw, []))
    blank_teams = set(blanks.get(gw, []))

    if check_tag_valid(tag, [gw], season, dbsession):
        budget = squad.sale_value(gw, use_api=False)
        optimal_squad = make_new_squad(
            [gw],
            tag,
            budget=budget,
            season=season,
            verbose=False,
            population_size=num_iterations,
            generations=num_iterations,
        )
        optimal_points = optimal_squad.get_expected_points(gw, tag)
        current_points = squad.get_expected_points(gw, tag)
        is_double = any(p.team in double_teams for p in squad.players) or any(
            p.team in double_teams for p in optimal_squad.players
        )
        return ChipValue(
            chip="free_hit",
            gameweek=gw,
            expected_gain=optimal_points - current_points,
            method="prediction",
            is_double=is_double,
            notes="optimum vs current squad, single GW",
        )

    n_blank = sum(1 for p in squad.players if p.team in blank_teams)
    gain = n_blank * AVG_STARTER_PTS + len(double_teams) * DGW_UPSIDE_PTS_PER_TEAM
    return ChipValue(
        chip="free_hit",
        gameweek=gw,
        expected_gain=gain,
        method="proxy",
        is_double=bool(double_teams),
        notes=f"{n_blank} squad players blank, {len(double_teams)} teams doubling",
    )


CHIP_ESTIMATORS = {
    "triple_captain": estimate_triple_captain_value,
    "bench_boost": estimate_bench_boost_value,
    "wildcard": estimate_wildcard_value,
    "free_hit": estimate_free_hit_value,
}


# --------------------------------------------------------------------------
# Decision rule (docs/chip_timing_spec.md §4.1)
# --------------------------------------------------------------------------
#
# NOTE on where this lives: docs/chip_timing_spec.md §8 nominally assigns
# recommend_chip_timing to PR3 (alongside optimiser integration), but §4.1
# places its signature/docstring in the same "New module" section as the
# estimators above, and §4.2's CLI example output includes a
# "RECOMMENDATION: HOLD/PLAY" line that only this function can produce.
# Resolved here (see this PR's description) by implementing the decision
# rule now, so airsenal_chip_report can produce real recommendations - but
# NOT wiring it into run_optimization/the pipeline (--chip_strategy,
# fill_transfersuggestion_table.py, airsenal_run_pipeline.py), which is left
# for PR3 as originally planned.


def _discounted_score(value: ChipValue, next_gw: int, proxy_penalty: float) -> float:
    """Discounted, uncertainty-penalised score used to rank/argmax
    ChipValues in the decision rule below. This is *not* what gets
    reported for display - the report shows each ChipValue's raw
    expected_gain (see airsenal/scripts/chip_report.py).
    """
    discount = get_discount_factor(next_gw, value.gameweek)
    penalty = proxy_penalty if value.method == "proxy" else 1.0
    return value.expected_gain * discount * penalty


def _recommend_for_window(
    window: ChipWindow,
    values: list[ChipValue],
    next_gw: int,
    horizon_gws: set[int],
    risk_lambda: float,
    proxy_penalty: float,
    excluded_gws: set[int],
) -> ChipRecommendation:
    """Build a single chip's recommendation from its precomputed per-GW
    ChipValues, treating any GW in ``excluded_gws`` (already claimed by a
    higher-priority chip, see recommend_chip_timing's conflict resolution)
    as unavailable.
    """
    candidates = [v for v in values if v.gameweek not in excluded_gws]
    if not candidates:
        # Nothing left to recommend (every candidate GW was claimed by
        # another chip) - hold, with no meaningful best_gw.
        return ChipRecommendation(
            chip=window.name,
            play_now=False,
            best_gw=window.end_gw,
            gain_now=0.0,
            best_future_gain=0.0,
            deadline_gw=window.end_gw,
            values=values,
        )

    in_horizon = [v for v in candidates if v.gameweek in horizon_gws]
    post_horizon = [v for v in candidates if v.gameweek not in horizon_gws]

    gain_now = max((v.expected_gain for v in in_horizon), default=0.0)

    if post_horizon:
        best_future_value = max(
            post_horizon, key=lambda v: _discounted_score(v, next_gw, proxy_penalty)
        )
        best_future_gain = _discounted_score(best_future_value, next_gw, proxy_penalty)
    else:
        best_future_gain = 0.0

    best_value = max(
        candidates, key=lambda v: _discounted_score(v, next_gw, proxy_penalty)
    )
    best_gw = best_value.gameweek

    play_now = gain_now >= risk_lambda * best_future_gain
    # Never let a chip expire unused: force a play if the deadline falls
    # inside the horizon and there is positive value in playing now.
    if window.end_gw in horizon_gws and gain_now > 0:
        play_now = True

    if play_now and in_horizon:
        # Report the best *in-horizon* gameweek as the target, not whichever
        # far-future GW happened to win the (discounted) argmax above.
        best_gw = max(in_horizon, key=lambda v: v.expected_gain).gameweek

    return ChipRecommendation(
        chip=window.name,
        play_now=play_now,
        best_gw=best_gw,
        gain_now=gain_now,
        best_future_gain=best_future_gain,
        deadline_gw=window.end_gw,
        values=values,
    )


def recommend_chip_timing(
    chip_windows: list[ChipWindow],
    squad: Squad,
    tag: str,
    gameweeks: list[int],
    season: str = CURRENT_SEASON,
    risk_lambda: float = 0.8,
    dbsession: Session = session,
) -> list[ChipRecommendation]:
    """For each currently-available chip:

    1. Compute a ChipValue for every GW from next_gw (min of ``gameweeks``)
       to its window's deadline.
    2. gain_now = max expected_gain over the in-horizon GWs (``gameweeks``).
    3. best_future_gain = max, over post-horizon GWs, of expected_gain *
       get_discount_factor(next_gw, gw) * proxy_penalty, where
       proxy_penalty (PROXY_UNCERTAINTY_PENALTY, default 0.9) reflects
       estimation uncertainty for long-range (proxy) values.
    4. play_now = gain_now >= risk_lambda * best_future_gain. Deadline
       pressure is handled naturally: as the deadline nears, the set of
       future GWs shrinks, so best_future_gain falls and play_now flips to
       True before the chip can expire. Additionally, play_now is forced
       True if the deadline is within the current horizon and gain_now > 0
       (never let a chip expire unused).
    5. Only one chip per GW: if two chips both say "play now" for the same
       GW, keep the one with the larger
       (gain_now - risk_lambda * best_future_gain) margin and defer the
       other by excluding that GW and recomputing its recommendation.

    If a chip has two windows (one per half-season) and both are marked
    available, only the currently "live" one (not yet past its deadline,
    preferring the earlier-starting window) is considered - the other is
    simply out of scope for a report about "now".
    """
    if not gameweeks:
        return []
    next_gw = min(gameweeks)
    horizon_gws = set(gameweeks)
    proxy_penalty = PROXY_UNCERTAINTY_PENALTY

    current_windows: dict[str, ChipWindow] = {}
    for window in chip_windows:
        if not window.available or window.end_gw < next_gw:
            continue
        existing = current_windows.get(window.name)
        if existing is None or window.start_gw < existing.start_gw:
            current_windows[window.name] = window

    per_chip_values: dict[str, list[ChipValue]] = {}
    for name, window in current_windows.items():
        estimator = CHIP_ESTIMATORS[name]
        gws = range(max(next_gw, window.start_gw), window.end_gw + 1)
        per_chip_values[name] = [
            estimator(g, squad, tag, season, dbsession) for g in gws
        ]

    excluded_gws: dict[str, set[int]] = {name: set() for name in current_windows}
    recommendations: dict[str, ChipRecommendation] = {
        name: _recommend_for_window(
            window,
            per_chip_values[name],
            next_gw,
            horizon_gws,
            risk_lambda,
            proxy_penalty,
            excluded_gws[name],
        )
        for name, window in current_windows.items()
    }

    # Resolve "only one chip per GW" conflicts among chips both recommended
    # to play_now for the same gameweek: keep the larger-margin chip, defer
    # the rest by excluding that GW and recomputing their recommendation.
    # Bounded to len(current_windows) passes - at most 4 chips, so this is
    # always enough to resolve any chain of conflicts.
    for _ in range(len(current_windows)):
        by_gw: dict[int, list[str]] = defaultdict(list)
        for name, rec in recommendations.items():
            if rec.play_now:
                by_gw[rec.best_gw].append(name)
        conflicts = {gw: names for gw, names in by_gw.items() if len(names) > 1}
        if not conflicts:
            break

        def _margin(name: str) -> float:
            rec = recommendations[name]
            return rec.gain_now - risk_lambda * rec.best_future_gain

        for gw, names in conflicts.items():
            winner = max(names, key=_margin)
            for name in names:
                if name == winner:
                    continue
                excluded_gws[name].add(gw)
                recommendations[name] = _recommend_for_window(
                    current_windows[name],
                    per_chip_values[name],
                    next_gw,
                    horizon_gws,
                    risk_lambda,
                    proxy_penalty,
                    excluded_gws[name],
                )

    return list(recommendations.values())
