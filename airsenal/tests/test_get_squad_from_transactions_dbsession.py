"""
Regression test for a real bug hit building the 2-team dashboard
(2026-08-10): get_squad_from_transactions/get_starting_squad hardcoded the
module-global `session` instead of accepting a dbsession parameter, making
it impossible to reconstruct a squad against a second, independent
database (e.g. a second FPL team's own AIRSENAL_HOME) from within the same
process. Both now accept and use an explicit dbsession.
"""

import inspect

import pytest

from airsenal.conftest import session_scope
from airsenal.framework.optimization_utils import (
    get_squad_from_transactions,
    get_starting_squad,
)
from airsenal.framework.schema import Transaction
from airsenal.framework.schema import session as global_session
from airsenal.framework.season import CURRENT_SEASON

SEASON = CURRENT_SEASON


def _add_buy_transaction(dbsession, player_id, fpl_team_id, gameweek=1, price=50):
    t = Transaction()
    t.player_id = player_id
    t.gameweek = gameweek
    t.bought_or_sold = 1
    t.season = SEASON
    t.time = "2026-08-01T12:00:00"
    t.tag = "test"
    t.price = price
    t.free_hit = 0
    t.fpl_team_id = fpl_team_id
    dbsession.add(t)


def test_get_squad_from_transactions_uses_explicit_dbsession(fill_players):
    """A transaction that only exists in an explicit dbsession (not the
    global default session) must still be found when that dbsession is
    passed in - and must NOT be found via the default global session,
    proving the parameter is actually used rather than silently ignored."""
    fpl_team_id = 555501
    with session_scope() as ts:
        _add_buy_transaction(ts, player_id=0, fpl_team_id=fpl_team_id)
        ts.commit()

        squad = get_squad_from_transactions(
            2, season=SEASON, fpl_team_id=fpl_team_id, dbsession=ts
        )
    assert len(squad.players) == 1
    assert squad.players[0].player_id == 0

    # the transaction only exists in `ts` above (a separate test.db engine),
    # not the module-global `session` (data.db) - confirms dbsession isn't
    # just being accepted and ignored.
    with pytest.raises(ValueError, match="No transactions in database"):
        get_squad_from_transactions(2, season=SEASON, fpl_team_id=fpl_team_id)


def test_get_starting_squad_uses_explicit_dbsession(fill_players):
    fpl_team_id = 555502
    with session_scope() as ts:
        _add_buy_transaction(ts, player_id=1, fpl_team_id=fpl_team_id)
        ts.commit()

        squad = get_starting_squad(
            next_gw=2,
            season=SEASON,
            fpl_team_id=fpl_team_id,
            use_api=False,
            dbsession=ts,
        )
    assert len(squad.players) == 1
    assert squad.players[0].player_id == 1


def test_get_starting_squad_default_dbsession_still_works(fill_players):
    """Sanity check: not passing dbsession still falls back to the global
    session as before (existing callers are unaffected)."""
    fpl_team_id = 555503
    with session_scope() as ts:
        _add_buy_transaction(ts, player_id=2, fpl_team_id=fpl_team_id, gameweek=1)
        ts.commit()

    # without dbsession, this call can't see the transaction added above
    # (it only exists in the separate test.db engine)
    with pytest.raises(ValueError, match="No transactions in database"):
        get_starting_squad(
            next_gw=2, season=SEASON, fpl_team_id=fpl_team_id, use_api=False
        )


def test_current_season_default_unaffected():
    """The dbsession parameter defaults to the existing module-global
    session, so every pre-existing caller (none of which pass dbsession)
    keeps working exactly as before."""
    sig = inspect.signature(get_starting_squad)
    assert sig.parameters["dbsession"].default is global_session
    assert sig.parameters["season"].default == CURRENT_SEASON
