"""
Tests for free-transfer and points-hit accounting:
- calc_free_transfers / calc_points_hit pure helpers (including the "Bx"/"Tx"
  bench-boost/triple-captain transfer codes and wildcard/free-hit cases).
- get_free_transfers banking from the Transaction table, which must respect the
  24/25 rule change allowing up to 5 (rather than 2) banked free transfers. This
  is the path used when replaying past seasons.
"""

import re

import pytest

from airsenal.conftest import session_scope
from airsenal.framework.optimization_utils import (
    MAX_FREE_TRANSFERS,
    calc_free_transfers,
    calc_points_hit,
)
from airsenal.framework.schema import Transaction
from airsenal.framework.utils import get_free_transfers


@pytest.mark.parametrize(
    ("num_transfers", "free_transfers", "expected"),
    [
        ("W", 1, 0),  # wildcard: no hit
        ("F", 0, 0),  # free hit: no hit
        (0, 1, 0),  # made fewer transfers than available
        (1, 2, 0),
        (2, 2, 0),  # exactly used all free transfers
        (3, 1, 8),  # 2 transfers beyond the 1 free => 8 point hit
        (4, 0, 16),
        ("B0", 1, 0),  # bench boost with 0 extra transfers
        ("B2", 1, 4),  # bench boost with 2 transfers, 1 free => 4 point hit
        ("T0", 0, 0),  # triple captain with 0 extra transfers
        ("T2", 0, 8),  # triple captain with 2 transfers, 0 free => 8 point hit
    ],
)
def test_calc_points_hit(num_transfers, free_transfers, expected):
    assert calc_points_hit(num_transfers, free_transfers) == expected


@pytest.mark.parametrize("bad", ["X", "B12", "WW"])
def test_calc_points_hit_invalid(bad):
    with pytest.raises(RuntimeError):
        calc_points_hit(bad, 1)


@pytest.mark.parametrize(
    ("num_transfers", "prev_free_transfers", "expected"),
    [
        ("W", 3, 3),  # wildcard preserves banked free transfers (24/25 rule)
        ("F", 5, 5),  # free hit preserves banked free transfers
        (0, 1, 2),  # no transfer used => bank one more
        (0, 5, 5),  # already at the max, cannot exceed it
        (1, 1, 1),  # used the gained transfer, stay at 1
        (2, 1, 1),  # used more than available, floor at 1
        (1, 5, 5),  # 1 + 5 - 1 = 5, still at the cap
        (2, 5, 4),  # using 2 from a full bank leaves 4
        ("B0", 3, 4),  # bench boost, 0 transfers => bank one more
        ("T2", 1, 1),  # triple captain with 2 transfers, floor at 1
    ],
)
def test_calc_free_transfers(num_transfers, prev_free_transfers, expected):
    assert (
        calc_free_transfers(num_transfers, prev_free_transfers, MAX_FREE_TRANSFERS)
        == expected
    )


@pytest.mark.parametrize("bad", ["X", "B12", "WW"])
def test_calc_free_transfers_invalid(bad):
    with pytest.raises(ValueError, match=re.escape(bad)):
        calc_free_transfers(bad, 1)


def _add_transactions(dbsession, fpl_team_id, buys_per_gw, season="2021"):
    """Insert 'bought' transactions for the given {gameweek: num_buys} mapping."""
    for gw, n_buys in buys_per_gw.items():
        for i in range(n_buys):
            dbsession.add(
                Transaction(
                    player_id=i,
                    gameweek=gw,
                    bought_or_sold=1,
                    season=season,
                    time="2020-08-01T00:00:00",
                    tag="test",
                    price=50,
                    free_hit=0,
                    fpl_team_id=fpl_team_id,
                )
            )
    dbsession.commit()


def test_get_free_transfers_no_transactions():
    """With no transactions for a team, default to 1 free transfer."""
    with session_scope() as ts:
        assert (
            get_free_transfers(
                fpl_team_id=9990001, gameweek=5, dbsession=ts, is_replay=True
            )
            == 1
        )


def test_get_free_transfers_banks_up_to_five():
    """
    Making no transfers for several gameweeks should bank free transfers up to the
    new maximum of 5 (the previous implementation incorrectly capped this at 2).
    """
    fpl_team_id = 9990002
    with session_scope() as ts:
        # Single buy at GW1 to establish the starting gameweek, then no transfers.
        _add_transactions(ts, fpl_team_id, {1: 1})
        # By GW8 (6 idle gameweeks) the bank should be capped at MAX_FREE_TRANSFERS.
        assert (
            get_free_transfers(
                fpl_team_id=fpl_team_id, gameweek=8, dbsession=ts, is_replay=True
            )
            == MAX_FREE_TRANSFERS
        )


def test_get_free_transfers_reduced_by_transfers_made():
    """A gameweek with 2 transfers should reduce the banked free transfers."""
    fpl_team_id = 9990003
    with session_scope() as ts:
        # GW1 start, idle GW2 (=> 2 FT), 2 transfers GW3 (=> 1 FT), idle GW4 (=> 2).
        _add_transactions(ts, fpl_team_id, {1: 1, 3: 2})
        assert (
            get_free_transfers(
                fpl_team_id=fpl_team_id, gameweek=5, dbsession=ts, is_replay=True
            )
            == 2
        )


def test_get_free_transfers_never_below_one():
    """Even spending more than the bank in a gameweek leaves at least 1."""
    fpl_team_id = 9990004
    with session_scope() as ts:
        _add_transactions(ts, fpl_team_id, {1: 1, 2: 4})
        assert (
            get_free_transfers(
                fpl_team_id=fpl_team_id, gameweek=3, dbsession=ts, is_replay=True
            )
            == 1
        )
