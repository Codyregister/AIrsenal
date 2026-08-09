"""
Tests for PR4 of docs/chip_timing_spec.md: the --chip_strategy (and
--risk_lambda / chip_gameweeks) passthrough from replay_season() to
run_optimization (see docs/chip_timing_spec.md §4.4).

A full replay re-simulates a season gameweek by gameweek, generating real
predictions and running the (multiprocessing, GA-based) transfer optimiser
each time - far too slow for the regular pytest suite. These tests instead
mock out everything except the single call to run_optimization for one
gameweek, and assert that replay_season() forwards chip_strategy,
chip_gameweeks and risk_lambda through unchanged, and that the "chip
actually played this gameweek" bookkeeping (needed so a chip_strategy
"manual" replay with --*_week 0 doesn't just replay the same chip every
week for the rest of the season) behaves as expected.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from airsenal.scripts import replay_season as rs

pytestmark = pytest.mark.usefixtures("_chdir_tmp_path")


@pytest.fixture
def _chdir_tmp_path(tmp_path, monkeypatch):
    """replay_season() writes {tag_prefix}.json to the current directory -
    keep that out of the repo root."""
    monkeypatch.chdir(tmp_path)


DEFAULT_CHIP_GAMEWEEKS = {
    "wildcard": -1,
    "free_hit": -1,
    "triple_captain": -1,
    "bench_boost": -1,
}


def _make_squad_mock() -> MagicMock:
    squad = MagicMock()
    squad.players = []
    squad.get_expected_points.return_value = 55.5
    squad.get_actual_points.return_value = 60
    return squad


def _best_strategy(gw: int, chip_played: str | None = None) -> dict:
    return {
        "free_transfers": {str(gw): 1},
        "num_transfers": {str(gw): 0},
        "points_hit": {str(gw): 0},
        "players_in": {str(gw): []},
        "players_out": {str(gw): []},
        "chips_played": {str(gw): chip_played},
    }


@contextmanager
def _dummy_session_scope():
    yield MagicMock()


def _run_single_gw_replay(
    run_optimization_return, chip_strategy="off", chip_gameweeks=None, risk_lambda=0.8
):
    """Run replay_season for a single gameweek (GW10), with everything
    except run_optimization mocked out, and return the mock so callers can
    inspect how it was called.
    """
    with (
        patch.object(rs, "session_scope", _dummy_session_scope),
        patch.object(rs, "get_gameweeks_array", return_value=[10, 11, 12]),
        patch.object(rs, "make_predictedscore_table", return_value="sometag"),
        patch.object(
            rs, "run_optimization", return_value=run_optimization_return
        ) as mock_run_opt,
    ):
        rs.replay_season(
            season="2324",
            gameweek_start=10,
            gameweek_end=10,
            new_squad=False,
            fpl_team_id=-1,
            tag_prefix="test_replay_season_pr4",
            chip_strategy=chip_strategy,
            chip_gameweeks=chip_gameweeks,
            risk_lambda=risk_lambda,
        )
    return mock_run_opt


class TestChipStrategyPassthrough:
    def test_off_is_forwarded(self):
        squad = _make_squad_mock()
        mock_run_opt = _run_single_gw_replay(
            (squad, _best_strategy(10)), chip_strategy="off"
        )
        _, kwargs = mock_run_opt.call_args
        assert kwargs["chip_strategy"] == "off"
        assert kwargs["chip_gameweeks"] == {}
        assert kwargs["risk_lambda"] == 0.8
        assert kwargs["is_replay"] is True

    def test_auto_is_forwarded(self):
        squad = _make_squad_mock()
        mock_run_opt = _run_single_gw_replay(
            (squad, _best_strategy(10)), chip_strategy="auto", risk_lambda=0.6
        )
        _, kwargs = mock_run_opt.call_args
        assert kwargs["chip_strategy"] == "auto"
        assert kwargs["risk_lambda"] == 0.6

    def test_manual_any_week_chip_gameweeks_forwarded(self):
        """The 'greedy' replay configuration: chip_strategy=manual with all
        four chip weeks set to 0 ('any week'), matching AIrsenal's
        pre-chip-timing default CLI usage.
        """
        squad = _make_squad_mock()
        greedy_weeks = {
            "wildcard": 0,
            "free_hit": 0,
            "triple_captain": 0,
            "bench_boost": 0,
        }
        mock_run_opt = _run_single_gw_replay(
            (squad, _best_strategy(10)),
            chip_strategy="manual",
            chip_gameweeks=greedy_weeks,
        )
        _, kwargs = mock_run_opt.call_args
        assert kwargs["chip_strategy"] == "manual"
        assert kwargs["chip_gameweeks"] == greedy_weeks
        # the dict passed to run_optimization must be a copy, not the same
        # object replay_season mutates internally between gameweeks
        assert kwargs["chip_gameweeks"] is not greedy_weeks


class TestChipUsageTracking:
    """run_optimization has no memory, across separate replay_season()
    calls to it, of chips played in earlier gameweeks of the same replay -
    chip_strategy="off"/"manual" pass a static chip_gameweeks dict with no
    DB-based availability check (unlike "auto", which infers availability
    from TransferSuggestion rows - see chip_timing._infer_availability_from_db).
    replay_season() must therefore itself stop offering a chip once it's
    actually been played, or a 'manual' replay with e.g. wildcard_week=0
    would happily "replay" the wildcard every single week for the rest of
    the season.
    """

    def test_played_chip_is_excluded_from_later_gameweeks(self):
        squad = _make_squad_mock()
        greedy_weeks = {
            "wildcard": 0,
            "free_hit": 0,
            "triple_captain": 0,
            "bench_boost": 0,
        }
        # simulate the optimiser actually committing to a wildcard this GW
        mock_run_opt = _run_single_gw_replay(
            (squad, _best_strategy(10, chip_played="wildcard")),
            chip_strategy="manual",
            chip_gameweeks=greedy_weeks,
        )
        # the dict passed in to *this* call still offered wildcard...
        _, kwargs = mock_run_opt.call_args
        assert kwargs["chip_gameweeks"]["wildcard"] == 0
        # ...but the caller's own dict must now be updated to never offer
        # wildcard again in a subsequent gameweek of the same replay.
        assert greedy_weeks["wildcard"] == -1
        # the other chips remain available
        assert greedy_weeks["free_hit"] == 0
        assert greedy_weeks["triple_captain"] == 0
        assert greedy_weeks["bench_boost"] == 0

    def test_no_chip_played_leaves_gameweeks_unchanged(self):
        squad = _make_squad_mock()
        greedy_weeks = {
            "wildcard": 0,
            "free_hit": 0,
            "triple_captain": 0,
            "bench_boost": 0,
        }
        _run_single_gw_replay(
            (squad, _best_strategy(10, chip_played=None)),
            chip_strategy="manual",
            chip_gameweeks=greedy_weeks,
        )
        assert greedy_weeks == {
            "wildcard": 0,
            "free_hit": 0,
            "triple_captain": 0,
            "bench_boost": 0,
        }


class TestOptimizationFailureFallback:
    """Regression tests for the 2223 replay re-run (2026-08-09): a
    genuinely blank gameweek can leave the tree search with zero valid
    strategies anywhere (every wildcard/free-hit candidate squad rebuild
    fails - see SquadOpt._check_positions_available /
    test_optimization_squad.py), which run_optimization now surfaces as a
    clean "Failed to find a strategy!" ValueError. replay_season() should
    not let that abort the whole season - it should fall back to no
    transfers for that one gameweek and keep going.
    """

    def test_falls_back_to_no_transfer_and_continues(self):
        squad = _make_squad_mock()
        with (
            patch.object(rs, "session_scope", _dummy_session_scope),
            patch.object(rs, "get_gameweeks_array", return_value=[10, 11, 12]),
            patch.object(rs, "make_predictedscore_table", return_value="sometag"),
            patch.object(
                rs,
                "run_optimization",
                side_effect=ValueError("Failed to find a strategy!"),
            ),
            patch.object(rs, "get_starting_squad", return_value=squad) as mock_gss,
        ):
            results = rs.replay_season(
                season="2324",
                gameweek_start=10,
                gameweek_end=10,
                new_squad=False,
                fpl_team_id=-1,
                tag_prefix="test_replay_season_fallback",
                chip_strategy="manual",
                chip_gameweeks={
                    "wildcard": 0,
                    "free_hit": 0,
                    "triple_captain": 0,
                    "bench_boost": 0,
                },
            )

        # fell back to fetching the existing squad unchanged, rather than
        # propagating the ValueError and aborting the replay
        mock_gss.assert_called_once_with(
            next_gw=10, season="2324", fpl_team_id=-1, use_api=False
        )
        gw_result = results["gameweeks"][0]
        assert gw_result["optimization_fallback"] is True
        assert gw_result["num_transfers"] == 0
        assert gw_result["points_hit"] == 0
        assert gw_result["players_in"] == []
        assert gw_result["players_out"] == []
        assert gw_result["chip_played"] is None

    def test_normal_gameweek_is_not_flagged_as_fallback(self):
        squad = _make_squad_mock()
        mock_run_opt = _run_single_gw_replay((squad, _best_strategy(10)))
        assert mock_run_opt.called
        # sanity check on the companion test above: a normal, successful
        # gameweek must NOT be flagged as a fallback
        with (
            patch.object(rs, "session_scope", _dummy_session_scope),
            patch.object(rs, "get_gameweeks_array", return_value=[10, 11, 12]),
            patch.object(rs, "make_predictedscore_table", return_value="sometag"),
            patch.object(
                rs, "run_optimization", return_value=(squad, _best_strategy(10))
            ),
        ):
            results = rs.replay_season(
                season="2324",
                gameweek_start=10,
                gameweek_end=10,
                new_squad=False,
                fpl_team_id=-1,
                tag_prefix="test_replay_season_no_fallback",
            )
        assert results["gameweeks"][0]["optimization_fallback"] is False


class TestReplayResultsOutput:
    """replay_season() should extend, not replace, the existing
    replay_results structure - see docs/chip_timing_spec.md §4.4.
    """

    def test_replay_results_records_chip_strategy_and_totals(self):
        squad = _make_squad_mock()
        results = None
        with (
            patch.object(rs, "session_scope", _dummy_session_scope),
            patch.object(rs, "get_gameweeks_array", return_value=[10, 11, 12]),
            patch.object(rs, "make_predictedscore_table", return_value="sometag"),
            patch.object(
                rs,
                "run_optimization",
                return_value=(squad, _best_strategy(10, chip_played="wildcard")),
            ),
        ):
            results = rs.replay_season(
                season="2324",
                gameweek_start=10,
                gameweek_end=10,
                new_squad=False,
                fpl_team_id=-1,
                tag_prefix="test_replay_season_pr4_results",
                chip_strategy="auto",
                risk_lambda=0.7,
            )
        assert results["chip_strategy"] == "auto"
        assert results["risk_lambda"] == 0.7
        assert results["total_actual_points"] == 60
        assert results["total_expected_points"] == 55.5
        assert results["chips_played"] == {10: "wildcard"}
