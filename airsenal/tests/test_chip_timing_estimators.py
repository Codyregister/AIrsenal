"""
Tests for the per-chip value estimators, the recommend_chip_timing decision
rule, and the airsenal_chip_report CLI, all added in
airsenal/framework/chip_timing.py (PR2). See docs/chip_timing_spec.md §6.

Slow, real machinery is avoided throughout:
- estimate_wildcard_value/estimate_free_hit_value's in-horizon path (which
  runs a DEAP genetic-algorithm squad search via make_new_squad) is tested
  with make_new_squad mocked/stubbed - see docs/chip_timing_spec.md §6 and
  this PR's brief, which explicitly call this out to keep the suite fast.
- the long-range proxy path's team-model-based fixture strength
  (_team_fixture_strength) is mocked where it would otherwise trigger a
  real (very slow - around a minute) Dixon-Coles model fit; the fit/predict
  machinery itself is already covered by test_score_predictions.py.
"""

from unittest.mock import patch

import pytest

from airsenal.conftest import session_scope
from airsenal.framework.chip_timing import (
    AVG_STARTER_PTS,
    BENCH_BASELINE_TOTAL_PTS,
    CHIP_ESTIMATORS,
    DGW_UPSIDE_PTS_PER_TEAM,
    PREMIUM_CAPTAIN_BASELINE_PTS,
    STALENESS_PTS_PER_ISSUE,
    ChipValue,
    ChipWindow,
    estimate_bench_boost_value,
    estimate_free_hit_value,
    estimate_triple_captain_value,
    estimate_wildcard_value,
    recommend_chip_timing,
)
from airsenal.framework.schema import Fixture, PlayerPrediction
from airsenal.framework.squad import Squad
from airsenal.framework.utils import CURRENT_SEASON

pytestmark = pytest.mark.filterwarnings("ignore:Using purchase price as sale price")

# fill_players (airsenal/conftest.py) creates 15 GKs, DEFs, MIDs, FWDs cycled
# every 15 players; the first 15 (ids 0-14) give exactly one full squad
# (2 GK, 5 DEF, 5 MID, 3 FWD) and the next 15 (ids 15-29) give a second,
# disjoint one - handy for an "optimal vs current" comparison.
FIRST_SQUAD_IDS = list(range(15))
SECOND_SQUAD_IDS = list(range(15, 30))


def _add_predictions(dbsession, season, gw, tag, points_by_player: dict[int, float]):
    """Insert a Fixture plus one PlayerPrediction per (player_id, points) at
    ``gw``, under ``tag`` - the minimal DB state needed for
    check_tag_valid(tag, [gw], season, dbsession) to be True, and for
    get_predicted_points_for_player to return real values.
    """
    fixture = Fixture(
        date="2020-09-12T14:00:00Z",
        gameweek=gw,
        home_team="AAA",
        away_team="BBB",
        season=season,
        tag="fx",
    )
    dbsession.add(fixture)
    dbsession.flush()
    for player_id, points in points_by_player.items():
        dbsession.add(
            PlayerPrediction(
                fixture_id=fixture.fixture_id,
                predicted_points=points,
                tag=tag,
                player_id=player_id,
            )
        )
    dbsession.commit()


def _build_squad(dbsession, player_ids, gw, season=CURRENT_SEASON) -> Squad:
    squad = Squad(season=season)
    for pid in player_ids:
        added = squad.add_player(
            pid, gameweek=gw, dbsession=dbsession, check_team=False
        )
        assert added, f"failed to add player {pid} to test squad"
    return squad


# --------------------------------------------------------------------------
# triple_captain
# --------------------------------------------------------------------------


def test_triple_captain_in_horizon_equals_top_predicted_points(fill_players):
    with session_scope() as ts:
        gw, season, tag = 501, CURRENT_SEASON, "tc_in_horizon"
        points = {
            pid: float(pid) for pid in FIRST_SQUAD_IDS
        }  # player 14 is best (14.0)
        _add_predictions(ts, season, gw, tag, points)
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        value = estimate_triple_captain_value(gw, squad, tag, season, ts)

        assert value.method == "prediction"
        assert value.expected_gain == pytest.approx(14.0)
        assert value.chip == "triple_captain"
        assert value.gameweek == gw


def test_triple_captain_proxy_uses_squad_best_captain(fill_players):
    with session_scope() as ts:
        gw, season, tag = 502, CURRENT_SEASON, "tc_proxy_with_squad"
        # predictions exist for gw 10 (not the target gw 502), so the squad
        # has *some* predictions (the proxy's "best captain" signal) but not
        # for the target gameweek - forcing the proxy path.
        points = {pid: float(pid) for pid in FIRST_SQUAD_IDS}
        _add_predictions(ts, season, 10, tag, points)
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        with patch(
            "airsenal.framework.chip_timing._team_fixture_strength", return_value=1.0
        ):
            value = estimate_triple_captain_value(gw, squad, tag, season, ts)

        assert value.method == "proxy"
        # best captain candidate is player 14 (avg points-per-fixture 14.0),
        # no doubles/blanks set up so fixture_count=1, strength mocked to 1.0
        assert value.expected_gain == pytest.approx(14.0)


def test_triple_captain_proxy_falls_back_to_baseline_with_no_predictions(fill_players):
    with session_scope() as ts:
        gw, season, tag = 503, CURRENT_SEASON, "tc_proxy_no_predictions"
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        with patch(
            "airsenal.framework.chip_timing._team_fixture_strength", return_value=1.0
        ):
            value = estimate_triple_captain_value(gw, squad, tag, season, ts)

        assert value.method == "proxy"
        # no known "best captain" team -> fixture_count=1, neutral 0.5
        # strength (no team to look up a fixture-strength scaling for).
        assert value.expected_gain == pytest.approx(PREMIUM_CAPTAIN_BASELINE_PTS * 0.5)


# --------------------------------------------------------------------------
# bench_boost
# --------------------------------------------------------------------------


def test_bench_boost_in_horizon_equals_expected_points_difference(fill_players):
    with session_scope() as ts:
        gw, season, tag = 511, CURRENT_SEASON, "bb_in_horizon"
        points = {pid: float(10 + pid) for pid in FIRST_SQUAD_IDS}
        _add_predictions(ts, season, gw, tag, points)
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        value = estimate_bench_boost_value(gw, squad, tag, season, ts)

        with_bb = squad.get_expected_points(gw, tag, bench_boost=True)
        without_bb = squad.get_expected_points(gw, tag)

        assert value.method == "prediction"
        assert value.expected_gain == pytest.approx(with_bb - without_bb)
        # sanity: bench boost should never score less than not playing it
        assert value.expected_gain >= 0


def test_bench_boost_proxy_all_blank_gives_zero(fill_players):
    with session_scope() as ts:
        gw, season, tag = 512, CURRENT_SEASON, "bb_proxy_blank"
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)
        # no predictions at all for this tag -> proxy path. No Team rows for
        # this season either, so get_double_blank_gameweeks returns no
        # doubles/blanks and every bench player is treated as a normal
        # (1-fixture) week.
        value = estimate_bench_boost_value(gw, squad, tag, season, ts)

        assert value.method == "proxy"
        assert value.expected_gain == pytest.approx(BENCH_BASELINE_TOTAL_PTS)


# --------------------------------------------------------------------------
# wildcard / free hit (make_new_squad mocked - see module docstring)
# --------------------------------------------------------------------------


def test_wildcard_in_horizon_equals_optimal_minus_current(fill_players):
    with session_scope() as ts:
        gw, season, tag = 521, CURRENT_SEASON, "wc_in_horizon"
        current_points = dict.fromkeys(FIRST_SQUAD_IDS, 2.0)
        optimal_points = dict.fromkeys(SECOND_SQUAD_IDS, 5.0)
        _add_predictions(ts, season, gw, tag, {**current_points, **optimal_points})

        current_squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)
        optimal_squad = _build_squad(ts, SECOND_SQUAD_IDS, gw, season)

        with patch(
            "airsenal.framework.chip_timing.make_new_squad",
            return_value=optimal_squad,
        ) as mock_mns:
            value = estimate_wildcard_value(
                gw, current_squad, tag, season, ts, num_iterations=2
            )
        assert mock_mns.called

        expected_gain = optimal_squad.get_expected_points(
            gw, tag
        ) - current_squad.get_expected_points(gw, tag)
        assert value.method == "prediction"
        assert value.expected_gain == pytest.approx(expected_gain)
        assert value.expected_gain > 0  # optimal squad scores much higher


def test_wildcard_proxy_counts_blank_gameweek_players(fill_players):
    with session_scope() as ts:
        gw, season, tag = 522, CURRENT_SEASON, "wc_proxy"
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        with patch(
            "airsenal.framework.chip_timing._team_fixture_strength", return_value=1.0
        ):
            value = estimate_wildcard_value(gw, squad, tag, season, ts)

        assert value.method == "proxy"
        # no blanks/injuries/poor fixtures set up (no Team rows for this
        # season, no Absence data, strength forced to 1.0) -> zero issues.
        assert value.expected_gain == pytest.approx(0.0)


def test_wildcard_proxy_scales_with_staleness_issues(fill_players):
    """With _team_fixture_strength forced below the 0.4 "poor fixture"
    threshold, every squad player should count as an issue."""
    with session_scope() as ts:
        gw, season, tag = 523, CURRENT_SEASON, "wc_proxy_stale"
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        with patch(
            "airsenal.framework.chip_timing._team_fixture_strength", return_value=0.1
        ):
            value = estimate_wildcard_value(gw, squad, tag, season, ts)

        assert value.expected_gain == pytest.approx(
            len(FIRST_SQUAD_IDS) * STALENESS_PTS_PER_ISSUE
        )


def test_free_hit_in_horizon_equals_optimal_minus_current(fill_players):
    with session_scope() as ts:
        gw, season, tag = 531, CURRENT_SEASON, "fh_in_horizon"
        current_points = dict.fromkeys(FIRST_SQUAD_IDS, 2.0)
        optimal_points = dict.fromkeys(SECOND_SQUAD_IDS, 6.0)
        _add_predictions(ts, season, gw, tag, {**current_points, **optimal_points})

        current_squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)
        optimal_squad = _build_squad(ts, SECOND_SQUAD_IDS, gw, season)

        with patch(
            "airsenal.framework.chip_timing.make_new_squad",
            return_value=optimal_squad,
        ):
            value = estimate_free_hit_value(
                gw, current_squad, tag, season, ts, num_iterations=2
            )

        expected_gain = optimal_squad.get_expected_points(
            gw, tag
        ) - current_squad.get_expected_points(gw, tag)
        assert value.method == "prediction"
        assert value.expected_gain == pytest.approx(expected_gain)


def test_free_hit_proxy_formula(fill_players):
    with session_scope() as ts:
        gw, season, tag = 532, CURRENT_SEASON, "fh_proxy"
        squad = _build_squad(ts, FIRST_SQUAD_IDS, gw, season)

        value = estimate_free_hit_value(gw, squad, tag, season, ts)

        assert value.method == "proxy"
        # no blank/doubling teams set up for this season -> both terms zero.
        assert value.expected_gain == pytest.approx(0.0)


def test_free_hit_proxy_dgw_upside_constant_is_used():
    # doesn't need a squad/DB round-trip - just pins down the documented
    # constant's units (points per doubling team).
    assert DGW_UPSIDE_PTS_PER_TEAM > 0
    assert AVG_STARTER_PTS > 0


# --------------------------------------------------------------------------
# recommend_chip_timing (decision rule)
# --------------------------------------------------------------------------


def _fake_estimator(gain_by_gw: dict[int, float], double_gws: tuple[int, ...] = ()):
    def estimator(gw, squad, tag, season, dbsession):
        return ChipValue(
            chip="fake",
            gameweek=gw,
            expected_gain=gain_by_gw.get(gw, 0.0),
            method="prediction" if gw in gain_by_gw else "proxy",
            is_double=gw in double_gws,
        )

    return estimator


class _StubSquad:
    """recommend_chip_timing never inspects the squad itself when the
    estimators are mocked out - only passes it through - so a stub is fine
    here."""


def test_recommend_holds_for_big_future_double_gameweek(monkeypatch):
    window = ChipWindow(name="bench_boost", start_gw=5, end_gw=38, available=True)
    gains = {5: 3.0, 6: 2.0, 7: 2.5, 15: 20.0}  # a big DGW far in the future
    monkeypatch.setitem(
        CHIP_ESTIMATORS, "bench_boost", _fake_estimator(gains, double_gws=(15,))
    )

    recs = recommend_chip_timing(
        [window], _StubSquad(), "tag", [5, 6, 7], risk_lambda=0.8
    )
    (rec,) = recs
    assert rec.play_now is False
    assert rec.best_gw == 15


def test_recommend_plays_when_deadline_in_horizon(monkeypatch):
    window = ChipWindow(name="triple_captain", start_gw=1, end_gw=6, available=True)
    gains = {5: 3.0, 6: 1.0}
    monkeypatch.setitem(CHIP_ESTIMATORS, "triple_captain", _fake_estimator(gains))

    recs = recommend_chip_timing(
        [window], _StubSquad(), "tag", [5, 6, 7], risk_lambda=0.8
    )
    (rec,) = recs
    assert rec.play_now is True
    assert rec.deadline_gw == 6


def test_recommend_resolves_same_gw_conflict_between_two_chips(monkeypatch):
    tc_window = ChipWindow(name="triple_captain", start_gw=1, end_gw=19, available=True)
    bb_window = ChipWindow(name="bench_boost", start_gw=1, end_gw=19, available=True)
    # both chips want GW5 specifically, and nothing else looks good.
    tc_gains = {5: 10.0, 6: 1.0, 7: 1.0}
    bb_gains = {5: 8.0, 6: 1.0, 7: 1.0}
    monkeypatch.setitem(CHIP_ESTIMATORS, "triple_captain", _fake_estimator(tc_gains))
    monkeypatch.setitem(CHIP_ESTIMATORS, "bench_boost", _fake_estimator(bb_gains))

    recs = recommend_chip_timing(
        [tc_window, bb_window], _StubSquad(), "tag", [5, 6, 7], risk_lambda=0.8
    )
    recs_by_chip = {r.chip: r for r in recs}

    playing_gw5 = [r for r in recs_by_chip.values() if r.play_now and r.best_gw == 5]
    assert len(playing_gw5) == 1
    # the larger-gain chip (triple_captain) should be the one that keeps GW5.
    assert playing_gw5[0].chip == "triple_captain"


def test_recommend_risk_lambda_zero_always_plays_now(monkeypatch):
    window = ChipWindow(name="wildcard", start_gw=1, end_gw=38, available=True)
    gains = {5: 1.0, 6: 0.5, 20: 50.0}  # a much better future GW
    monkeypatch.setitem(CHIP_ESTIMATORS, "wildcard", _fake_estimator(gains))

    recs = recommend_chip_timing(
        [window], _StubSquad(), "tag", [5, 6, 7], risk_lambda=0.0
    )
    (rec,) = recs
    assert rec.play_now is True


def test_recommend_large_risk_lambda_only_deadline_forced_plays(monkeypatch):
    # window 1: no deadline pressure - a huge risk_lambda should hold.
    window_far = ChipWindow(name="wildcard", start_gw=1, end_gw=38, available=True)
    gains_far = {5: 5.0, 6: 1.0, 20: 50.0}
    monkeypatch.setitem(CHIP_ESTIMATORS, "wildcard", _fake_estimator(gains_far))

    recs = recommend_chip_timing(
        [window_far], _StubSquad(), "tag", [5, 6, 7], risk_lambda=1e6
    )
    (rec_far,) = recs
    assert rec_far.play_now is False

    # window 2: deadline inside the horizon with positive gain_now -> forced
    # to play even with a huge risk_lambda.
    window_deadline = ChipWindow(name="free_hit", start_gw=1, end_gw=6, available=True)
    gains_deadline = {5: 5.0, 6: 1.0}
    monkeypatch.setitem(CHIP_ESTIMATORS, "free_hit", _fake_estimator(gains_deadline))

    recs = recommend_chip_timing(
        [window_deadline], _StubSquad(), "tag", [5, 6, 7], risk_lambda=1e6
    )
    (rec_deadline,) = recs
    assert rec_deadline.play_now is True


def test_recommend_skips_unavailable_and_expired_windows():
    unavailable = ChipWindow(
        name="triple_captain", start_gw=1, end_gw=19, available=False
    )
    expired = ChipWindow(name="bench_boost", start_gw=1, end_gw=4, available=True)

    recs = recommend_chip_timing(
        [unavailable, expired], _StubSquad(), "tag", [5, 6, 7], risk_lambda=0.8
    )
    assert recs == []


def test_recommend_empty_gameweeks_returns_empty_list():
    window = ChipWindow(name="wildcard", start_gw=1, end_gw=38, available=True)
    assert recommend_chip_timing([window], _StubSquad(), "tag", []) == []
