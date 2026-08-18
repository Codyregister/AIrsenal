"""
Regression test for a real incident (2026-08-17), continued: alongside the
login() prompt fixed in test_fetcher.py, airsenal_run_pipeline had its own
input() prompt on a failed DB update, with the same hang risk under cron.
Fixed the same way (skip the prompt, warn and continue, when stdin isn't
interactive) - this test exercises that specific branch with everything
else mocked out, since a full pipeline run needs real network/DB access.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

from airsenal.scripts import airsenal_run_pipeline as arp


@contextmanager
def _dummy_session_scope():
    yield MagicMock()


def _run_pipeline(monkeypatch, *, update_ok: bool, isatty: bool):
    input_mock = MagicMock(side_effect=AssertionError("input() should not be called"))
    monkeypatch.setattr(arp, "set_multiprocessing_start_method", lambda: None)
    monkeypatch.setattr(arp, "session_scope", _dummy_session_scope)
    monkeypatch.setattr(arp, "check_clean_db", lambda *a, **k: False)
    monkeypatch.setattr(arp, "update_database", lambda *a, **k: update_ok)
    monkeypatch.setattr(arp, "run_prediction", lambda *a, **k: True)
    monkeypatch.setattr(arp, "has_local_squad_history", lambda *a, **k: True)
    monkeypatch.setattr(arp, "get_entry_start_gameweek", lambda *a, **k: -999)
    monkeypatch.setattr(arp, "run_optimize_squad", lambda *a, **k: True)
    monkeypatch.setattr(arp, "run_make_squad", lambda *a, **k: True)
    monkeypatch.setattr(arp, "get_gameweeks_array", lambda *a, **k: [1, 2, 3])
    monkeypatch.setattr(arp.sys.stdin, "isatty", lambda: isatty)
    monkeypatch.setattr("builtins.input", input_mock)

    arp.run_pipeline.callback(
        num_thread=1,
        weeks_ahead=3,
        fpl_team_id=742663,
        clean=False,
        apply_transfers=False,
        wildcard_week=-1,
        free_hit_week=-1,
        triple_captain_week=-1,
        bench_boost_week=-1,
        chip_strategy="off",
        risk_lambda=0.8,
        n_previous=3,
        no_current_season=False,
        team_model="extended",
        epsilon=0.9,
        max_transfers=2,
        max_hit=8,
        allow_unused=False,
        save_absences=False,
    )
    return input_mock


def test_failed_db_update_does_not_block_when_not_interactive(monkeypatch, capsys):
    """The core fix: a failed DB update with no one able to answer the
    confirmation prompt must not hang - it should warn and continue."""
    _run_pipeline(monkeypatch, update_ok=False, isatty=False)
    out = capsys.readouterr().out
    assert "Pipeline finished OK!" in out


def test_successful_db_update_never_prompts(monkeypatch, capsys):
    """Sanity check: the happy path (update succeeds) never even reaches
    the prompt logic, interactive or not."""
    _run_pipeline(monkeypatch, update_ok=True, isatty=True)
    out = capsys.readouterr().out
    assert "Pipeline finished OK!" in out
