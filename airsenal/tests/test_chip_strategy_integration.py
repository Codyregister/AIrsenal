"""
Tests for PR3 of docs/chip_timing_spec.md: wiring --chip_strategy into
run_optimization (airsenal/scripts/fill_transfersuggestion_table.py) and
airsenal_run_pipeline.

Two layers of tests:

1. resolve_auto_chip_gameweeks / chip_weeks_manually_set are pure-ish
   functions (their only side effect is one call out to
   chip_report.build_chip_report) and are tested directly, with
   build_chip_report mocked out - the recommendation logic itself is
   already covered by test_chip_timing.py/test_chip_report.py, so these
   tests only need to check how run_optimization *uses* a recommendation.

2. run_optimization itself spins up real multiprocessing Process objects
   running a genetic-algorithm-based transfer search against a real
   squad/predictions - too slow and DB-heavy for a unit test, and not what
   this PR changed (that machinery is unchanged). So the run_optimization
   tests below replace `Process` with a stand-in that just records its
   constructor args (in particular chip_gw_dict, the dict handed to the
   tree search) rather than starting a subprocess, and stub out the other
   DB/network-touching calls (get_starting_squad, get_free_transfers).
   Because no strategy is ever actually computed this way,
   run_optimization always fails after that point - it has a pre-existing
   (not introduced by this PR) quirk where fill_suggestion_table(None) is
   called before the "Failed to find a strategy!" ValueError check, which
   raises TypeError instead. Tests using the fixture below expect that,
   and only assert on state captured earlier (the recorded Process args).
"""

from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from airsenal.framework.squad import Squad
from airsenal.scripts import fill_transfersuggestion_table as fts

DEFAULT_CHIP_GAMEWEEKS = {
    "wildcard": -1,
    "free_hit": -1,
    "triple_captain": -1,
    "bench_boost": -1,
}


def _report(chips: dict) -> dict:
    """Build a build_chip_report()-shaped dict from
    {chip_name: recommendation_dict_or_None}."""
    return {
        "season": "2526",
        "tag": "tag",
        "next_gw": 10,
        "chips": {
            name: {"window": {}, "values": [], "recommendation": rec}
            for name, rec in chips.items()
        },
    }


def _rec(play_now: bool, best_gw: int) -> dict:
    return {
        "play_now": play_now,
        "best_gw": best_gw,
        "gain_now": 5.0,
        "best_future_gain": 1.0,
        "deadline_gw": 19,
    }


class TestChipWeeksManuallySet:
    def test_all_default_is_not_manual(self):
        assert not fts.chip_weeks_manually_set(DEFAULT_CHIP_GAMEWEEKS)

    def test_positive_week_is_manual(self):
        assert fts.chip_weeks_manually_set({**DEFAULT_CHIP_GAMEWEEKS, "wildcard": 5})

    def test_any_week_zero_is_manual(self):
        assert fts.chip_weeks_manually_set({**DEFAULT_CHIP_GAMEWEEKS, "free_hit": 0})


class TestResolveAutoChipGameweeks:
    def test_pins_play_now_chip_inside_horizon(self, monkeypatch):
        report = _report(
            {
                "wildcard": _rec(True, 11),
                "free_hit": _rec(False, 30),
                "bench_boost": None,
                "triple_captain": None,
            }
        )
        monkeypatch.setattr(fts, "build_chip_report", lambda **kw: report)
        monkeypatch.setattr(fts, "format_report_text", lambda *_a, **_kw: "REPORT TEXT")

        resolved, text = fts.resolve_auto_chip_gameweeks(
            dict(DEFAULT_CHIP_GAMEWEEKS),
            gameweeks=[10, 11, 12],
            season="2526",
            tag="tag",
            fpl_team_id=1,
        )

        assert resolved["wildcard"] == 11
        assert resolved["free_hit"] == -1
        assert resolved["bench_boost"] == -1
        assert resolved["triple_captain"] == -1
        assert text == "REPORT TEXT"

    def test_play_now_but_best_gw_outside_horizon_is_not_pinned(self, monkeypatch):
        # Defensive: recommend_chip_timing shouldn't return play_now with a
        # best_gw outside the gameweeks horizon it was given, but this
        # function must not blindly trust that invariant either.
        report = _report({"wildcard": _rec(True, 99)})
        monkeypatch.setattr(fts, "build_chip_report", lambda **kw: report)
        monkeypatch.setattr(fts, "format_report_text", lambda *_a, **_kw: "REPORT TEXT")

        resolved, _text = fts.resolve_auto_chip_gameweeks(
            {"wildcard": -1},
            gameweeks=[10, 11, 12],
            season="2526",
            tag="tag",
            fpl_team_id=1,
        )

        assert resolved["wildcard"] == -1

    def test_hold_recommendation_leaves_chip_unplayed(self, monkeypatch):
        report = _report({"triple_captain": _rec(False, 34)})
        monkeypatch.setattr(fts, "build_chip_report", lambda **kw: report)
        monkeypatch.setattr(fts, "format_report_text", lambda *_a, **_kw: "REPORT TEXT")

        resolved, _text = fts.resolve_auto_chip_gameweeks(
            {"triple_captain": -1},
            gameweeks=[10, 11, 12],
            season="2526",
            tag="tag",
            fpl_team_id=1,
        )

        assert resolved["triple_captain"] == -1

    def test_falls_back_to_off_and_warns_on_recommender_failure(self, monkeypatch):
        def _boom(**kw):
            msg = "FPL API down"
            raise RuntimeError(msg)

        monkeypatch.setattr(fts, "build_chip_report", _boom)

        with pytest.warns(UserWarning, match="Could not compute chip timing"):
            resolved, text = fts.resolve_auto_chip_gameweeks(
                dict(DEFAULT_CHIP_GAMEWEEKS),
                gameweeks=[10, 11, 12],
                season="2526",
                tag="tag",
                fpl_team_id=1,
            )

        assert resolved == DEFAULT_CHIP_GAMEWEEKS
        assert text is None


class _FakeProcess:
    """Stand-in for multiprocessing.Process that records its constructor
    args instead of spawning a subprocess. Used to observe the
    chip_gw_dict handed to the (unstarted) tree search without paying for
    a real optimisation run."""

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
    monkeypatch.setattr(fts, "get_entry_start_gameweek", lambda *a, **k: -999)
    return _FakeProcess.calls


def _run_and_capture_chip_dict(process_calls, **kwargs):
    """Run run_optimization far enough to construct its Process pool
    (which captures chip_gw_dict as the 6th positional arg to `optimize`),
    then let the pre-existing "no strategy found" failure happen (see
    module docstring) rather than working around it."""
    kwargs.setdefault("num_thread", 1)
    with pytest.raises(TypeError):
        fts.run_optimization(**kwargs)
    assert process_calls, "Process was never constructed"
    return process_calls[0][5]


def test_chip_strategy_off_never_calls_resolver(process_calls, monkeypatch):
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[10, 11, 12],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_strategy="off",
    )

    resolver.assert_not_called()
    for gw in (10, 11, 12):
        assert chip_gw_dict[gw]["chip_to_play"] is None


def test_chip_strategy_manual_never_calls_resolver(process_calls, monkeypatch):
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[10, 11, 12],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_strategy="manual",
    )

    resolver.assert_not_called()
    for gw in (10, 11, 12):
        assert chip_gw_dict[gw]["chip_to_play"] is None


def test_chip_strategy_manual_with_explicit_week_is_respected(
    process_calls, monkeypatch
):
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[10, 11, 12],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_gameweeks={**DEFAULT_CHIP_GAMEWEEKS, "bench_boost": 12},
        chip_strategy="manual",
    )

    resolver.assert_not_called()
    assert chip_gw_dict[12]["chip_to_play"] == "bench_boost"


def test_chip_strategy_auto_calls_resolver_and_pins_recommended_chip(
    process_calls, monkeypatch
):
    def fake_resolver(
        chip_gameweeks, gameweeks, season, tag, fpl_team_id, risk_lambda=0.8
    ):
        return {**chip_gameweeks, "wildcard": 11}, "REPORT"

    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", fake_resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[10, 11, 12],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_strategy="auto",
    )

    assert chip_gw_dict[11]["chip_to_play"] == "wildcard"


def test_chip_strategy_auto_respects_manual_override(process_calls, monkeypatch):
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[10, 11, 12],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_gameweeks={**DEFAULT_CHIP_GAMEWEEKS, "wildcard": 10},
        chip_strategy="auto",
    )

    # An explicit --wildcard_week flag always wins, even under
    # --chip_strategy auto - the recommender must not even be consulted.
    resolver.assert_not_called()
    assert chip_gw_dict[10]["chip_to_play"] == "wildcard"


def test_season_start_skips_chip_resolution_entirely(monkeypatch):
    # gameweeks[0] == 1 triggers the "start of a season / new team" branch,
    # which returns a freshly-built squad before any chip logic runs at
    # all - recommend_chip_timing must never be consulted for a team with
    # no squad yet (docs/chip_timing_spec.md §5 "Season start").
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)
    fake_squad = MagicMock()
    monkeypatch.setattr(fts, "fill_initial_squad", lambda **kw: fake_squad)

    squad, strat = fts.run_optimization(
        gameweeks=[1, 2, 3],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_strategy="auto",
    )

    resolver.assert_not_called()
    assert squad is fake_squad
    assert strat is None


def test_new_team_first_week_skips_chip_resolution_entirely(process_calls, monkeypatch):
    # A team that exists in FPL but has no squad/transactions in our DB
    # yet (get_starting_squad raises) takes the same "build from scratch"
    # early-return path, and must also skip chip resolution.
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)

    def _raise(*a, **k):
        msg = "no squad"
        raise ValueError(msg)

    monkeypatch.setattr(fts, "get_starting_squad", _raise)
    fake_squad = MagicMock()
    monkeypatch.setattr(fts, "fill_initial_squad", lambda **kw: fake_squad)

    squad, strat = fts.run_optimization(
        gameweeks=[10, 11, 12],
        tag="tag",
        season="2526",
        fpl_team_id=123,
        chip_strategy="auto",
    )

    resolver.assert_not_called()
    assert squad is fake_squad
    assert strat is None


def test_2223_world_cup_hack_overrides_auto_recommendation(process_calls, monkeypatch):
    def fake_resolver(
        chip_gameweeks, gameweeks, season, tag, fpl_team_id, risk_lambda=0.8
    ):
        # auto would like to play triple_captain in gw17...
        return {**chip_gameweeks, "triple_captain": 17}, "REPORT"

    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", fake_resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[17, 18, 19],
        tag="tag",
        season="2223",
        fpl_team_id=123,
        chip_strategy="auto",
    )

    # ...but the 2223 World Cup hack always wins for gw17 that season.
    assert chip_gw_dict[17]["chip_to_play"] == "wildcard"


def test_2223_world_cup_hack_applies_with_chip_strategy_off(process_calls, monkeypatch):
    resolver = MagicMock()
    monkeypatch.setattr(fts, "resolve_auto_chip_gameweeks", resolver)

    chip_gw_dict = _run_and_capture_chip_dict(
        process_calls,
        gameweeks=[17, 18, 19],
        tag="tag",
        season="2223",
        fpl_team_id=123,
        chip_strategy="off",
    )

    resolver.assert_not_called()
    assert chip_gw_dict[17]["chip_to_play"] == "wildcard"
