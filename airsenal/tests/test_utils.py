"""
test some db access helper functions
"""

import pytest

from airsenal.conftest import TEST_PAST_SEASON, past_data_session_scope, session_scope
from airsenal.framework.schema import Fixture, Player, Result, Transaction
from airsenal.framework.utils import (
    get_gameweek_by_date,
    get_last_complete_gameweek_in_db,
    get_player,
    get_player_id,
    get_player_name,
    get_reference_date_for_gameweek,
    get_return_gameweek_by_date,
    has_local_squad_history,
)


def test_get_reference_date_for_gameweek_handles_blank_gameweek():
    """Regression test for a real 2022-23 crash (World Cup fixture
    disruption): a genuine blank gameweek (no fixtures at all) used to
    crash callers that did `.min()` over the target gameweek's own fixture
    dates with no guard for zero fixtures. Should fall back to the nearest
    surrounding gameweek's earliest dated fixture instead."""
    season = "9903"
    with session_scope() as ts:
        fixture = Fixture()
        fixture.date = "2022-08-01T15:00:00Z"
        fixture.gameweek = 1
        fixture.home_team = "TEAMA"
        fixture.away_team = "TEAMB"
        fixture.season = season
        fixture.tag = "test"
        ts.add(fixture)
        ts.flush()

        result = Result()
        result.fixture_id = fixture.fixture_id
        result.home_score = 1
        result.away_score = 0
        ts.add(result)
        ts.commit()

        # gameweek 2 is a genuine blank - no Fixture rows exist for it at all
        ref_date = get_reference_date_for_gameweek(2, season, ts)

    assert ref_date.date().isoformat() == "2022-08-01"


def test_get_reference_date_for_gameweek_raises_clear_error_with_nothing_nearby():
    season = "9904"
    with (
        session_scope() as ts,
        pytest.raises(ValueError, match="No fixtures with dates found"),
    ):
        get_reference_date_for_gameweek(1, season, ts)


def test_get_player_name(fill_players):
    """
    Should be able to find a player with integer argument
    """
    with session_scope() as tsession:
        assert get_player_name(1, tsession) == "Bob"


def test_get_player_id(fill_players):
    """
    Should be able to find a player with string argument
    """
    with session_scope() as tsession:
        assert get_player_id("Bob", tsession) == 1


def test_get_player(fill_players):
    """
    test we can get a player object from either a name or an id
    """
    with session_scope() as tsession:
        p = get_player("Bob", tsession)
        assert isinstance(p, Player)
        assert p.player_id == 1


def test_get_return_gameweek_by_date():
    with past_data_session_scope() as ts:
        gw = get_return_gameweek_by_date(
            "2020-09-18", "ARS", season=TEST_PAST_SEASON, dbsession=ts
        )
        assert gw == 2

        gw = get_return_gameweek_by_date(
            "2020-09-20T12:34:00Z", "ARS", season=TEST_PAST_SEASON, dbsession=ts
        )
        assert gw == 3


def test_get_gameweek_by_date():
    with past_data_session_scope() as ts:
        gw = get_gameweek_by_date(
            "2020-09-20T12:34:00Z", season=TEST_PAST_SEASON, dbsession=ts
        )
        assert gw == 2


def test_get_last_complete_gameweek_in_db():
    with past_data_session_scope() as ts:
        gw = get_last_complete_gameweek_in_db(season=TEST_PAST_SEASON, dbsession=ts)
        assert gw == 5


def test_has_local_squad_history():
    """Regression test: airsenal_run_pipeline used to decide "brand new
    team, build a fresh squad" purely from get_entry_start_gameweek, which
    can't distinguish a genuinely new team from one that already has a
    squad recorded locally but the season hasn't started yet (its lookup
    loop never runs pre-season - see its docstring) - so it silently
    discarded and rebuilt an existing squad every single pipeline run
    during the whole pre-season period. has_local_squad_history is the
    extra check that fixes this."""
    season = "9906"
    fpl_team_id = 555504
    assert has_local_squad_history(fpl_team_id, season=season) is False

    with session_scope() as ts:
        t = Transaction()
        t.player_id = 1
        t.gameweek = 1
        t.bought_or_sold = 1
        t.season = season
        t.time = "2026-08-01T12:00:00"
        t.tag = "test"
        t.price = 50
        t.free_hit = 0
        t.fpl_team_id = fpl_team_id
        ts.add(t)
        ts.commit()

        assert has_local_squad_history(fpl_team_id, season=season, dbsession=ts) is True

    # a different team/season with no transactions of its own is unaffected
    assert has_local_squad_history(555505, season=season) is False
    assert has_local_squad_history(fpl_team_id, season="9907") is False
