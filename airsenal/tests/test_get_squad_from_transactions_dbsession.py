"""
Regression test for a real bug hit building the 2-team dashboard
(2026-08-10): get_squad_from_transactions/get_starting_squad hardcoded the
module-global `session` instead of accepting a dbsession parameter, making
it impossible to reconstruct a squad against a second, independent
database (e.g. a second FPL team's own AIRSENAL_HOME) from within the same
process. Both now accept and use an explicit dbsession.

Second follow-up regression (2026-08-19): get_squad_from_transactions filters
`Transaction.gameweek < gameweek`. There's no gameweek 0, so a brand new
squad's initial "buy" transactions are recorded at gameweek=1 - querying
"squad for gameweek 1" therefore always excluded them and looked
indistinguishable from "no squad exists". This crashed a live pre-season
run inside print_team_for_next_gw (fill_transfersuggestion_table.py), which
calls get_starting_squad(next_gw=1, ...) with no fallback of its own -
two other call sites (run_optimization, dashboard_app.py) had already
worked around the same boundary ad-hoc. Fixed once, centrally, by treating
gameweek=1 queries as if gameweek=2 was requested.

Follow-up regression (2026-08-18): threading dbsession through
unconditionally (defaulting to the module-global `session`, not None)
meant every CandidatePlayer in the returned Squad got a live SQLAlchemy
Session attached, even for callers that never asked for one. That's fine
single-process, but broke the tree-search optimiser in
fill_transfersuggestion_table.py, which sends whole Squad objects across a
multiprocessing Queue - Session objects can't be pickled. Crashed the
first ever live (non-replay) run of that code path with a PicklingError.
Fixed by defaulting to None and only attaching a session to the returned
players when the caller explicitly provided one.
"""

import inspect
import pickle

import pytest

import airsenal.framework.optimization_utils as optimization_utils
import airsenal.framework.utils as fw_utils
from airsenal.conftest import session_scope
from airsenal.framework.optimization_utils import (
    get_squad_from_transactions,
    get_starting_squad,
)
from airsenal.framework.schema import Transaction
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
    """season still defaults to CURRENT_SEASON as before."""
    sig = inspect.signature(get_starting_squad)
    assert sig.parameters["season"].default == CURRENT_SEASON


def test_default_dbsession_is_none_not_global_session():
    """dbsession must default to None, not the module-global `session` -
    see the module docstring's follow-up regression. Querying still falls
    back to the global session internally either way (proven by the other
    tests here), but the raw parameter value matters: it's what gets
    attached to each CandidatePlayer, and only None is picklable."""
    sig = inspect.signature(get_starting_squad)
    assert sig.parameters["dbsession"].default is None
    sig = inspect.signature(get_squad_from_transactions)
    assert sig.parameters["dbsession"].default is None


def test_default_call_produces_picklable_players(fill_players, monkeypatch):
    """The actual regression: a Squad reconstructed without an explicit
    dbsession (the truly default path - no override at all) must be
    picklable (as required by the multiprocessing tree-search optimiser) -
    i.e. no live Session object attached to any CandidatePlayer. Uses the
    isolated session_scope() engine (which has the fill_players fixture
    data) monkeypatched in as the module's own "no override" default,
    since the real module-global `session` is a separate, unseeded
    database in this test environment (bound to a different file than
    session_scope()'s engine)."""
    fpl_team_id = 555508
    with session_scope() as ts:
        _add_buy_transaction(ts, player_id=0, fpl_team_id=fpl_team_id)
        ts.commit()

        monkeypatch.setattr(optimization_utils, "session", ts)
        monkeypatch.setattr(fw_utils, "session", ts)
        squad = get_starting_squad(
            next_gw=2, season=SEASON, fpl_team_id=fpl_team_id, use_api=False
        )
    assert squad.players[0].dbsession is None
    pickle.dumps(squad)  # must not raise


def test_gameweek_one_query_includes_gameweek_one_transactions(fill_players):
    """The second follow-up regression: a brand new squad's initial buy
    transactions are recorded at gameweek=1 (there's no gameweek 0).
    Querying get_squad_from_transactions(gameweek=1, ...) must still
    return them - previously the strict `gameweek < 1` filter excluded
    them and raised ValueError, indistinguishable from "no squad"."""
    fpl_team_id = 555509
    with session_scope() as ts:
        _add_buy_transaction(ts, player_id=0, fpl_team_id=fpl_team_id, gameweek=1)
        ts.commit()

        squad = get_squad_from_transactions(
            1, season=SEASON, fpl_team_id=fpl_team_id, dbsession=ts
        )
    assert len(squad.players) == 1
    assert squad.players[0].player_id == 0


def test_explicit_dbsession_call_attaches_it_to_players(fill_players):
    """The other half: a caller that DOES explicitly pass a dbsession (e.g.
    the dashboard, querying a second team's own database) still gets it
    attached to the returned players, same as before this fix."""
    fpl_team_id = 555507
    with session_scope() as ts:
        _add_buy_transaction(ts, player_id=4, fpl_team_id=fpl_team_id)
        ts.commit()

        squad = get_starting_squad(
            next_gw=2,
            season=SEASON,
            fpl_team_id=fpl_team_id,
            use_api=False,
            dbsession=ts,
        )
        assert squad.players[0].dbsession is ts
