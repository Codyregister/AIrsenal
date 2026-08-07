"""
Tests for airsenal/framework/price_change.py and
airsenal/scripts/fill_price_change_snapshot.py.
"""

from airsenal.conftest import session_scope
from airsenal.framework.price_change import get_price_momentum, status_label
from airsenal.framework.schema import PriceChangeSnapshot


def _add_snapshot(
    dbsession,
    player_id,
    season,
    date,
    transfers_in,
    transfers_out,
    price=55,
    selected_by_percent=10.0,
):
    snap = PriceChangeSnapshot()
    snap.player_id = player_id
    snap.season = season
    snap.snapshot_date = date
    snap.price = price
    snap.transfers_in = transfers_in
    snap.transfers_out = transfers_out
    snap.selected_by_percent = selected_by_percent
    snap.timestamp = f"{date}T12:00:00"
    dbsession.add(snap)


def test_get_price_momentum_needs_two_days():
    """A player with only one day of history has nothing to diff against -
    should be silently excluded, not error."""
    season = "price_test_one_day"
    with session_scope() as ts:
        snap = PriceChangeSnapshot()
        snap.player_id = 1
        snap.season = season
        snap.snapshot_date = "2026-08-07"
        snap.price = 55
        snap.transfers_in = 100
        snap.transfers_out = 50
        snap.selected_by_percent = 10.0
        snap.timestamp = "2026-08-07T12:00:00"
        ts.add(snap)
        ts.commit()

        results = get_price_momentum(season=season, dbsession=ts)
    assert results == []


def test_get_price_momentum_computes_delta():
    season = "price_test_two_day"
    with session_scope() as ts:
        # player 1: gaining transfers (rising momentum)
        _add_snapshot(ts, 1, season, "2026-08-06", transfers_in=1000, transfers_out=500)
        _add_snapshot(ts, 1, season, "2026-08-07", transfers_in=1600, transfers_out=550)
        # player 2: losing transfers (falling momentum)
        _add_snapshot(ts, 2, season, "2026-08-06", transfers_in=500, transfers_out=200)
        _add_snapshot(ts, 2, season, "2026-08-07", transfers_in=520, transfers_out=900)
        ts.commit()

        results = get_price_momentum(season=season, dbsession=ts)

    by_player = {r.player_id: r for r in results}
    assert set(by_player) == {1, 2}

    p1 = by_player[1]
    # (1600-550) - (1000-500) = 1050 - 500 = 550
    assert p1.net_transfers_today == 550
    assert p1.momentum_pct > 0
    assert p1.days_of_history == 2
    assert p1.snapshot_date == "2026-08-07"

    p2 = by_player[2]
    # (520-900) - (500-200) = -380 - 300 = -680
    assert p2.net_transfers_today == -680
    assert p2.momentum_pct < 0

    # sorted descending by momentum - rising player first
    assert results[0].player_id == 1
    assert results[-1].player_id == 2


def test_get_price_momentum_uses_latest_two_snapshots():
    """With 3+ days of history, only the most recent two should be diffed -
    not e.g. the first and last."""
    season = "price_test_three_day"
    with session_scope() as ts:
        _add_snapshot(ts, 1, season, "2026-08-05", transfers_in=0, transfers_out=0)
        _add_snapshot(ts, 1, season, "2026-08-06", transfers_in=5000, transfers_out=0)
        _add_snapshot(ts, 1, season, "2026-08-07", transfers_in=5100, transfers_out=0)
        ts.commit()

        results = get_price_momentum(season=season, dbsession=ts)

    assert len(results) == 1
    # (5100-0) - (5000-0) = 100, NOT (5100-0) - (0-0) = 5100
    assert results[0].net_transfers_today == 100
    assert results[0].days_of_history == 3


def test_status_label_thresholds():
    assert status_label(10) == "strong rising momentum"
    assert status_label(2) == "rising momentum"
    assert status_label(0) == "little momentum"
    assert status_label(-2) == "falling momentum"
    assert status_label(-10) == "strong falling momentum"
