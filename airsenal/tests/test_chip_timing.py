"""
Tests for airsenal/framework/chip_timing.py:
- get_chip_windows: parsing the bootstrap-static "chips" JSON, API name
  mapping, availability from the (mocked) API, the pre-2025/26 season
  fallback windows, and availability inferred from the database when the API
  is unavailable.
- get_double_blank_gameweeks: double/blank gameweek detection from synthetic
  Fixture rows, including exclusion of unscheduled (gameweek=None) fixtures.
"""

import json
from pathlib import Path

import pytest

from airsenal.conftest import session_scope
from airsenal.framework.chip_timing import (
    ChipRecommendation,
    ChipValue,
    ChipWindow,
    get_chip_windows,
    get_double_blank_gameweeks,
)
from airsenal.framework.data_fetcher import FPLDataFetcher
from airsenal.framework.mappings import alternative_team_names
from airsenal.framework.schema import Fixture, Team, Transaction, TransferSuggestion
from airsenal.framework.schema import session as global_session
from airsenal.framework.utils import CURRENT_SEASON

TESTDATA_DIR = Path(__file__).parent / "testdata"

ALL_CHIP_NAMES = {"wildcard", "free_hit", "bench_boost", "triple_captain"}


def _windows_by_name(windows: list[ChipWindow]) -> dict[str, list[ChipWindow]]:
    out: dict[str, list[ChipWindow]] = {}
    for w in windows:
        out.setdefault(w.name, []).append(w)
    return out


class FakeFetcher(FPLDataFetcher):
    """FPLDataFetcher stand-in that serves canned data instead of hitting the
    network, so tests never require network access or FPL login."""

    def __init__(
        self,
        summary_data: dict | None = None,
        available_chips: list[str] | None = None,
        raise_on_availability: bool = False,
    ):
        super().__init__(fpl_team_id=123456)
        self._summary_data = summary_data
        self._available_chips = available_chips or []
        self._raise_on_availability = raise_on_availability

    def get_current_summary_data(self):
        if self._summary_data is None:
            msg = "no network access in tests"
            raise RuntimeError(msg)
        return self._summary_data

    def get_available_chips(self, _fpl_team_id=None):
        if self._raise_on_availability:
            msg = "no network access in tests"
            raise RuntimeError(msg)
        return self._available_chips


@pytest.fixture
def chips_json() -> dict:
    with (TESTDATA_DIR / "bootstrap_static_chips.json").open() as f:
        return json.load(f)


# --------------------------------------------------------------------------
# get_chip_windows: parsing + name mapping + availability from the API
# --------------------------------------------------------------------------


def test_get_chip_windows_parses_api_fixture_and_maps_names(chips_json):
    fetcher_stub = FakeFetcher(summary_data=chips_json)
    windows = get_chip_windows(
        fpl_team_id=123456,
        season=CURRENT_SEASON,
        apifetcher=fetcher_stub,
        use_api=True,
    )
    by_name = _windows_by_name(windows)
    assert set(by_name) == ALL_CHIP_NAMES
    assert sorted((w.start_gw, w.end_gw) for w in by_name["wildcard"]) == [
        (2, 19),
        (20, 38),
    ]
    assert sorted((w.start_gw, w.end_gw) for w in by_name["free_hit"]) == [
        (2, 19),
        (20, 38),
    ]
    assert sorted((w.start_gw, w.end_gw) for w in by_name["bench_boost"]) == [
        (1, 19),
        (20, 38),
    ]
    assert sorted((w.start_gw, w.end_gw) for w in by_name["triple_captain"]) == [
        (1, 19),
        (20, 38),
    ]


def test_get_chip_windows_availability_from_api(chips_json):
    fetcher_stub = FakeFetcher(
        summary_data=chips_json, available_chips=["wildcard", "bboost"]
    )
    windows = get_chip_windows(
        fpl_team_id=123456,
        season=CURRENT_SEASON,
        apifetcher=fetcher_stub,
        use_api=True,
    )
    available_names = {w.name for w in windows if w.available}
    unavailable_names = {w.name for w in windows if not w.available}
    assert available_names == {"wildcard", "bench_boost"}
    assert unavailable_names == {"free_hit", "triple_captain"}


def test_get_chip_windows_api_failure_falls_back_with_warning(chips_json):
    fetcher_stub = FakeFetcher(summary_data=None, raise_on_availability=True)
    with pytest.warns(UserWarning, match="Failed to get chip windows"):
        windows = get_chip_windows(
            fpl_team_id=None,
            season=CURRENT_SEASON,
            apifetcher=fetcher_stub,
            use_api=True,
        )
    # Falls back to the hardcoded (current-rules) windows.
    by_name = _windows_by_name(windows)
    assert set(by_name) == ALL_CHIP_NAMES
    assert sorted((w.start_gw, w.end_gw) for w in by_name["wildcard"]) == [
        (2, 19),
        (20, 38),
    ]
    # And falls back to DB inference for availability (no chips played
    # recorded, so everything defaults to available).
    assert all(w.available for w in windows)


def test_get_chip_windows_use_api_false_skips_api_entirely(chips_json):
    # Even though the fixture has valid data, use_api=False must not touch it.
    fetcher_stub = FakeFetcher(summary_data=chips_json, available_chips=["wildcard"])
    windows = get_chip_windows(
        fpl_team_id=123456,
        season=CURRENT_SEASON,
        apifetcher=fetcher_stub,
        use_api=False,
    )
    by_name = _windows_by_name(windows)
    assert set(by_name) == ALL_CHIP_NAMES
    # hardcoded windows only have entries for the season's rule set, not what
    # was in the (ignored) fixture.
    assert all(w.available for w in windows)


# --------------------------------------------------------------------------
# get_chip_windows: pre-2025/26 season fallback windows
# --------------------------------------------------------------------------


def test_get_chip_windows_pre_2526_season_fallback_windows():
    # A past season never hits the live API (which only reflects the current
    # season), so this exercises the hardcoded fallback windows directly.
    fetcher_stub = FakeFetcher(summary_data=None)
    windows = get_chip_windows(
        fpl_team_id=None, season="2021", apifetcher=fetcher_stub, use_api=True
    )
    by_name = _windows_by_name(windows)
    assert sorted((w.start_gw, w.end_gw) for w in by_name["wildcard"]) == [
        (2, 19),
        (20, 38),
    ]
    # free_hit/bench_boost/triple_captain only had a single window per season
    # before 2025/26.
    assert [(w.start_gw, w.end_gw) for w in by_name["free_hit"]] == [(2, 38)]
    assert [(w.start_gw, w.end_gw) for w in by_name["bench_boost"]] == [(1, 38)]
    assert [(w.start_gw, w.end_gw) for w in by_name["triple_captain"]] == [(1, 38)]


# --------------------------------------------------------------------------
# get_chip_windows: availability inferred from the database
# --------------------------------------------------------------------------


@pytest.fixture
def clean_global_chip_tables():
    """The DB-inference path in get_chip_windows uses the module-level
    default `session` (the function signature has no dbsession parameter),
    so exercise it directly against that session rather than the isolated
    session_scope() used elsewhere - and clean up afterwards so it doesn't
    leak into other tests.
    """
    yield global_session
    global_session.rollback()
    global_session.query(TransferSuggestion).delete()
    global_session.query(Transaction).delete()
    global_session.commit()


def test_get_chip_windows_availability_inferred_from_db(clean_global_chip_tables):
    db = clean_global_chip_tables
    season = "2021"
    fpl_team_id = 987654

    # Wildcard was played (per a previous optimisation run) in GW5, which
    # falls inside its first window (GW2-19) - so that window should be
    # marked unavailable, while the second wildcard window (GW20-38) should
    # remain available.
    db.add(
        TransferSuggestion(
            player_id=1,
            in_or_out=1,
            gameweek=5,
            points_gain=1.0,
            timestamp="2020-09-01",
            season=season,
            fpl_team_id=fpl_team_id,
            chip_played="wildcard",
        )
    )
    # Free hit inferred from a Transaction.free_hit flag instead of
    # TransferSuggestion, in GW10 (within its single pre-2526 window 2-38).
    db.add(
        Transaction(
            player_id=2,
            gameweek=10,
            bought_or_sold=1,
            season=season,
            time="2020-11-01T00:00:00",
            tag="test",
            price=50,
            free_hit=1,
            fpl_team_id=fpl_team_id,
        )
    )
    db.commit()

    fetcher_stub = FakeFetcher(summary_data=None)
    windows = get_chip_windows(
        fpl_team_id=fpl_team_id, season=season, apifetcher=fetcher_stub, use_api=True
    )
    by_name = _windows_by_name(windows)

    wildcard_first, wildcard_second = sorted(
        by_name["wildcard"], key=lambda w: w.start_gw
    )
    assert wildcard_first.available is False
    assert wildcard_second.available is True

    (free_hit_window,) = by_name["free_hit"]
    assert free_hit_window.available is False

    # bench_boost/triple_captain were never played, so remain available.
    assert all(w.available for w in by_name["bench_boost"])
    assert all(w.available for w in by_name["triple_captain"])


def test_get_chip_windows_db_availability_scoped_to_team(clean_global_chip_tables):
    """Chip usage recorded against a different fpl_team_id shouldn't affect
    availability for the team being queried."""
    db = clean_global_chip_tables
    season = "2021"
    db.add(
        TransferSuggestion(
            player_id=1,
            in_or_out=1,
            gameweek=5,
            points_gain=1.0,
            timestamp="2020-09-01",
            season=season,
            fpl_team_id=111,
            chip_played="wildcard",
        )
    )
    db.commit()

    fetcher_stub = FakeFetcher(summary_data=None)
    windows = get_chip_windows(
        fpl_team_id=222, season=season, apifetcher=fetcher_stub, use_api=True
    )
    assert all(w.available for w in windows)


# --------------------------------------------------------------------------
# get_double_blank_gameweeks
# --------------------------------------------------------------------------


def _add_fixture(dbsession, home, away, gameweek, season):
    dbsession.add(
        Fixture(
            date="2020-09-12T14:00:00Z",
            gameweek=gameweek,
            home_team=home,
            away_team=away,
            season=season,
            tag="test",
        )
    )


def test_get_double_blank_gameweeks_detects_doubles_and_blanks():
    # A season string unique to this test, so Fixture/Team rows (which
    # persist for the whole test session in the shared test DB) can't leak
    # into or be polluted by other tests.
    season = "chip_dgw_test_1"
    teams = list(alternative_team_names.keys())
    assert len(teams) >= 4

    with session_scope() as ts:
        # need Team rows for list_teams() to return the full team list
        for t in teams:
            ts.add(Team(name=t, full_name=t, season=season, team_id=1))
        ts.commit()

        # GW1: every team plays once (no doubles/blanks).
        pairs = list(zip(teams[::2], teams[1::2], strict=True))
        for home, away in pairs:
            _add_fixture(ts, home, away, 1, season)

        # GW2: first team plays twice (double gameweek), last team doesn't
        # play at all (blank gameweek).
        double_team = teams[0]
        blank_team = teams[-1]
        other_teams = [t for t in teams if t not in (double_team, blank_team)]
        _add_fixture(ts, double_team, other_teams[0], 2, season)
        _add_fixture(ts, other_teams[1], double_team, 2, season)
        remaining = other_teams[2:]
        for home, away in zip(remaining[::2], remaining[1::2], strict=True):
            _add_fixture(ts, home, away, 2, season)

        # An unscheduled fixture (gameweek=None) shouldn't affect detection.
        _add_fixture(ts, teams[2], teams[3], None, season)

        ts.commit()

        doubles, blanks = get_double_blank_gameweeks(season=season, dbsession=ts)

        assert 1 not in doubles
        assert 1 not in blanks
        assert double_team in doubles.get(2, [])
        assert blank_team in blanks.get(2, [])
        assert double_team not in blanks.get(2, [])
        assert blank_team not in doubles.get(2, [])


def test_get_double_blank_gameweeks_excludes_unscheduled_fixtures():
    season = "chip_dgw_test_2"
    teams = list(alternative_team_names.keys())[:4]

    with session_scope() as ts:
        for t in teams:
            ts.add(Team(name=t, full_name=t, season=season, team_id=1))
        ts.commit()

        # A single scheduled fixture for GW1 between the first two teams.
        _add_fixture(ts, teams[0], teams[1], 1, season)
        # An unscheduled fixture involving the other two teams: must not
        # count towards either team's fixture count for any gameweek, and
        # must not itself create a spurious "gameweek" entry.
        _add_fixture(ts, teams[2], teams[3], None, season)
        ts.commit()

        doubles, blanks = get_double_blank_gameweeks(season=season, dbsession=ts)

        # teams[2] and teams[3] have zero *scheduled* fixtures in every real
        # gameweek, so they should show up as blank in GW1 (and beyond).
        assert teams[2] in blanks.get(1, [])
        assert teams[3] in blanks.get(1, [])
        assert not doubles


def test_get_double_blank_gameweeks_no_fixtures_returns_empty_dicts():
    season = "9999_empty"
    with session_scope() as ts:
        doubles, blanks = get_double_blank_gameweeks(season=season, dbsession=ts)
    assert doubles == {}
    assert blanks == {}


# --------------------------------------------------------------------------
# Dataclass shape checks (nothing constructs these yet outside tests, but PR2
# builds on top of them so the shape needs to be right).
# --------------------------------------------------------------------------


def test_chip_window_dataclass():
    w = ChipWindow(name="wildcard", start_gw=2, end_gw=19, available=True)
    assert w.name == "wildcard"
    assert w.start_gw == 2
    assert w.end_gw == 19
    assert w.available is True


def test_chip_value_dataclass_defaults():
    v = ChipValue(
        chip="triple_captain",
        gameweek=10,
        expected_gain=7.5,
        method="prediction",
        is_double=False,
    )
    assert v.notes == ""


def test_chip_recommendation_dataclass():
    value = ChipValue(
        chip="bench_boost",
        gameweek=15,
        expected_gain=12.0,
        method="proxy",
        is_double=True,
    )
    rec = ChipRecommendation(
        chip="bench_boost",
        play_now=False,
        best_gw=15,
        gain_now=4.0,
        best_future_gain=12.0,
        deadline_gw=19,
        values=[value],
    )
    assert rec.values == [value]
