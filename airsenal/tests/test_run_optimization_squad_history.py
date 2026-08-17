"""
Regression tests for a real incident (2026-08-17): run_optimization's
"start of the season, build from scratch" check fired purely from
`gameweeks[0] == 1`, regardless of whether the team already had a squad
recorded locally. This silently discarded and rebuilt a deliberately-built
squad on every single automated pre-season run - even after the
equivalent check one level up, in airsenal_run_pipeline.py, was fixed
(TODO.md / has_local_squad_history), because this is a separate check.
Caught by actually watching the real scheduled weekly cron run rather than
assuming the earlier fix was sufficient.
"""

from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from airsenal.framework.squad import Squad
from airsenal.scripts import fill_transfersuggestion_table as fts


class _FakeProcess:
    """Stand-in for multiprocessing.Process - records constructor args
    instead of spawning a subprocess (same pattern as
    test_chip_strategy_integration.py)."""

    calls: ClassVar[list] = []

    def __init__(self, target=None, args=(), **kwargs):
        self.target = target
        type(self).calls.append(args)
        self.daemon = False

    def start(self):
        pass

    def join(self):
        pass


@pytest.fixture
def process_calls(monkeypatch):
    _FakeProcess.calls = []
    monkeypatch.setattr(fts, "Process", _FakeProcess)
    monkeypatch.setattr(fts, "get_free_transfers", lambda *a, **k: 1)
    monkeypatch.setattr(fts, "get_starting_squad", lambda *a, **k: Squad())
    monkeypatch.setattr(fts, "save_baseline_score", lambda *a, **k: None)
    return _FakeProcess.calls


def test_gameweek_one_with_existing_squad_does_not_rebuild_from_scratch(
    process_calls, monkeypatch
):
    """The core bug: gameweeks[0] == 1 alone used to be treated as 'no
    squad exists yet', even when has_local_squad_history says otherwise."""
    monkeypatch.setattr(fts, "has_local_squad_history", lambda *a, **k: True)
    monkeypatch.setattr(fts, "get_entry_start_gameweek", lambda *a, **k: -999)
    fill_initial_squad_mock = MagicMock()
    monkeypatch.setattr(fts, "fill_initial_squad", fill_initial_squad_mock)

    # let it fail past the Process pool setup (no real strategy found, same
    # "no strategy" quirk exercised in test_chip_strategy_integration.py)
    # rather than mocking the entire tree search.
    with pytest.raises(ValueError, match="Failed to find a strategy"):
        fts.run_optimization(
            gameweeks=[1, 2, 3],
            tag="tag",
            season="2627",
            fpl_team_id=742663,
            num_thread=1,
        )

    fill_initial_squad_mock.assert_not_called()
    assert process_calls, "Process pool was never constructed"


def test_gameweek_one_with_no_squad_history_still_builds_from_scratch(monkeypatch):
    """Sanity check the other half of the fix: a genuinely new team (no
    local squad history) at gameweek 1 must still take the scratch-build
    path - this must not regress into never building an initial squad."""
    monkeypatch.setattr(fts, "has_local_squad_history", lambda *a, **k: False)
    monkeypatch.setattr(fts, "get_entry_start_gameweek", lambda *a, **k: -999)
    squad = Squad()
    fill_initial_squad_mock = MagicMock(return_value=squad)
    monkeypatch.setattr(fts, "fill_initial_squad", fill_initial_squad_mock)

    result_squad, best_strategy = fts.run_optimization(
        gameweeks=[1, 2, 3],
        tag="tag",
        season="2627",
        fpl_team_id=742663,
        num_thread=1,
    )

    fill_initial_squad_mock.assert_called_once()
    assert result_squad is squad
    assert best_strategy is None


def test_gameweek_one_with_squad_history_but_via_entry_start_gameweek(monkeypatch):
    """has_local_squad_history must gate both ways into the "start of
    season" check - gameweeks[0] == get_entry_start_gameweek(...) is the
    other disjunct, not just gameweeks[0] == 1."""
    monkeypatch.setattr(fts, "has_local_squad_history", lambda *a, **k: True)
    monkeypatch.setattr(fts, "get_entry_start_gameweek", lambda *a, **k: 10)
    fill_initial_squad_mock = MagicMock()
    monkeypatch.setattr(fts, "fill_initial_squad", fill_initial_squad_mock)
    monkeypatch.setattr(fts, "Process", _FakeProcess)
    monkeypatch.setattr(fts, "get_free_transfers", lambda *a, **k: 1)
    monkeypatch.setattr(fts, "get_starting_squad", lambda *a, **k: Squad())
    monkeypatch.setattr(fts, "save_baseline_score", lambda *a, **k: None)
    _FakeProcess.calls = []

    with pytest.raises(ValueError, match="Failed to find a strategy"):
        fts.run_optimization(
            gameweeks=[10, 11, 12],
            tag="tag",
            season="2627",
            fpl_team_id=742663,
            num_thread=1,
        )

    fill_initial_squad_mock.assert_not_called()
