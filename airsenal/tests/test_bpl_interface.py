"""
Tests for airsenal/framework/bpl_interface.py.
"""

import pytest

from airsenal.conftest import session_scope
from airsenal.framework.bpl_interface import get_result_dict
from airsenal.framework.schema import Fixture, Result


def test_get_result_dict_handles_blank_gameweek():
    """Regression test for a real 2022-23 crash: get_result_dict used to do
    `np.array([...]).min()` over the target gameweek's own fixture dates with
    no guard for zero fixtures, so a genuine blank gameweek (no fixtures at
    all, e.g. World Cup fixture disruption) raised
    `ValueError: zero-size array to reduction operation minimum which has no
    identity`. It should now fall back to the nearest surrounding
    gameweek's date instead of crashing.
    """
    season = "9901"
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
        result_dict = get_result_dict(season=season, gameweek=2, dbsession=ts)

    assert len(result_dict["time_diff"]) == 1
    # falls back to gameweek 1's date (the only dated fixture in the
    # season), so the one result in training data is exactly "now"
    assert result_dict["time_diff"][0] == pytest.approx(0.0)


def test_get_result_dict_raises_clear_error_with_no_fixtures_anywhere():
    """If there really are no dated fixtures anywhere near the target
    gameweek, fail with a clear error rather than crashing on an empty
    array reduction."""
    season = "9902"
    with (
        session_scope() as ts,
        pytest.raises(ValueError, match="No fixtures with dates found"),
    ):
        get_result_dict(season=season, gameweek=1, dbsession=ts)
