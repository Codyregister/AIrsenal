"""
Smoke tests for airsenal_chip_report (airsenal/scripts/chip_report.py).

build_chip_report/get_starting_squad/get_chip_windows all read and write via
the *global* default session (airsenal.framework.schema.session), not the
isolated per-test session_scope() used elsewhere in the test suite - see
airsenal/framework/chip_timing.py's note that get_chip_windows has no
dbsession parameter for the same reason. So, like
test_chip_timing.py's clean_global_chip_tables fixture, these tests write
directly to (and clean up after themselves in) the global session.

The slow real team-model fit and GA-based wildcard/free-hit search are
mocked out throughout - see test_chip_timing_estimators.py's module
docstring for why.
"""

import json
from unittest.mock import patch

import pytest
from curl_cffi import requests

from airsenal.framework.schema import (
    Fixture,
    Player,
    PlayerAttributes,
    PlayerPrediction,
    Transaction,
)
from airsenal.framework.schema import session as global_session
from airsenal.framework.squad import Squad
from airsenal.framework.utils import CURRENT_SEASON, fetcher
from airsenal.scripts.chip_report import build_chip_report, format_report_text, main

TEST_SEASON = "chip_report_cli_test"
TEST_TAG = "chip_report_cli_tag"
TEST_FPL_TEAM_ID = 918273
PLAYER_IDS = list(range(970001, 970016))  # 15 players: 2 GK, 5 DEF, 5 MID, 3 FWD
POSITIONS = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3


def _seed_squad(db, season, fpl_team_id, player_ids, gw, transaction_gw, tag, points):
    """Add a minimal, self-contained squad + predictions to the global
    session: Player/PlayerAttributes/Transaction rows so
    get_starting_squad(use_api=False) can reconstruct a squad, plus a
    Fixture/PlayerPrediction under ``tag`` at ``gw`` so that gameweek is
    "in-horizon" for the estimators.
    """
    fixture = Fixture(
        date="2020-09-12T14:00:00Z",
        gameweek=gw,
        home_team="AAA",
        away_team="BBB",
        season=season,
        tag="fx",
    )
    db.add(fixture)
    db.flush()

    for player_id, position in zip(player_ids, POSITIONS, strict=True):
        db.add(Player(player_id=player_id, name=f"ChipReportPlayer{player_id}"))
        db.add(
            PlayerAttributes(
                player_id=player_id,
                season=season,
                gameweek=1,
                price=45,
                team="AAA" if player_id % 2 == 0 else "BBB",
                position=position,
            )
        )
        db.add(
            Transaction(
                player_id=player_id,
                gameweek=transaction_gw,
                bought_or_sold=1,
                season=season,
                time="2020-08-01T00:00:00",
                tag="init",
                price=45,
                free_hit=0,
                fpl_team_id=fpl_team_id,
            )
        )
        db.add(
            PlayerPrediction(
                fixture_id=fixture.fixture_id,
                predicted_points=points,
                tag=tag,
                player_id=player_id,
            )
        )
    db.commit()


def _clear_season(db, season, tag, fpl_team_id, player_ids):
    db.rollback()
    db.query(PlayerPrediction).filter(PlayerPrediction.tag == tag).delete()
    db.query(Transaction).filter(Transaction.fpl_team_id == fpl_team_id).delete()
    db.query(PlayerAttributes).filter(PlayerAttributes.season == season).delete()
    db.query(Player).filter(Player.player_id.in_(player_ids)).delete(
        synchronize_session=False
    )
    db.query(Fixture).filter(Fixture.season == season).delete()
    db.commit()


@pytest.fixture
def global_chip_report_env():
    db = global_session
    _seed_squad(
        db,
        TEST_SEASON,
        TEST_FPL_TEAM_ID,
        PLAYER_IDS,
        gw=5,
        transaction_gw=1,
        tag=TEST_TAG,
        points=5.0,
    )
    yield
    _clear_season(db, TEST_SEASON, TEST_TAG, TEST_FPL_TEAM_ID, PLAYER_IDS)


def _stub_make_new_squad(gw_range, _tag, **kwargs):
    """Stand-in for the real GA squad search: build a complete (if
    arbitrary) squad from the same player pool the tests seed, so
    Squad.get_expected_points has a valid 15-player squad to work with."""
    squad = Squad(season=kwargs.get("season", TEST_SEASON))
    for player_id in PLAYER_IDS:
        squad.add_player(player_id, gameweek=gw_range[0], check_team=False)
    return squad


@pytest.fixture
def fast_proxies():
    """Stub out the two expensive code paths a full-window chip report
    would otherwise hit: the team-model fit behind the long-range proxy,
    and the GA squad search behind the wildcard/free-hit in-horizon value.
    """
    with (
        patch(
            "airsenal.framework.chip_timing._team_fixture_strength",
            return_value=0.6,
        ),
        patch(
            "airsenal.framework.chip_timing.make_new_squad",
            side_effect=_stub_make_new_squad,
        ) as mock_mns,
    ):
        yield mock_mns


def test_build_chip_report_text_and_json(global_chip_report_env, fast_proxies):
    report = build_chip_report(
        fpl_team_id=TEST_FPL_TEAM_ID,
        season=TEST_SEASON,
        tag=TEST_TAG,
        weeks_ahead=3,
        risk_lambda=0.8,
        gameweeks=[5, 6, 7],
    )

    assert report["season"] == TEST_SEASON
    assert report["next_gw"] == 5
    assert set(report["chips"]) == {
        "wildcard",
        "free_hit",
        "bench_boost",
        "triple_captain",
    }
    for chip_data in report["chips"].values():
        assert "window" in chip_data
        assert "values" in chip_data

    # must be JSON-serialisable (used by --json)
    text = json.dumps(report)
    assert TEST_SEASON in text

    rendered = format_report_text(report)
    assert "TRIPLE CAPTAIN" in rendered
    assert "BENCH BOOST" in rendered
    assert "WILDCARD" in rendered
    assert "FREE HIT" in rendered
    assert "RECOMMENDATION:" in rendered


def test_build_chip_report_raises_for_missing_squad(global_chip_report_env):
    with pytest.raises(ValueError, match="No existing squad"):
        build_chip_report(
            fpl_team_id=555555555,  # no transactions for this team
            season=TEST_SEASON,
            tag=TEST_TAG,
            gameweeks=[5, 6, 7],
        )


def test_main_smoke_success(fast_proxies, monkeypatch, capsys):
    """End-to-end smoke test of the actual CLI entry point (argv parsing,
    build_chip_report, and both text/json rendering), using CURRENT_SEASON
    (the CLI's default) - non-current seasons need an explicit
    gameweek_start that this CLI doesn't expose. Network calls are stubbed
    so this stays fast and deterministic regardless of sandboxing; the
    squad needs a transaction gameweek before NEXT_GAMEWEEK, so 0 is used
    (this test environment starts at gameweek 1).
    """

    def _no_network(*_args, **_kwargs):
        msg = "no network access in tests"
        raise requests.exceptions.RequestException(msg)

    monkeypatch.setattr(fetcher, "get_current_summary_data", _no_network)
    monkeypatch.setattr(fetcher, "get_available_chips", _no_network)
    monkeypatch.setattr(fetcher, "get_current_picks", _no_network)

    db = global_session
    _seed_squad(
        db,
        CURRENT_SEASON,
        TEST_FPL_TEAM_ID,
        PLAYER_IDS,
        gw=1,
        transaction_gw=0,
        tag=TEST_TAG,
        points=5.0,
    )
    try:
        monkeypatch.setattr(
            "sys.argv",
            [
                "airsenal_chip_report",
                "--fpl_team_id",
                str(TEST_FPL_TEAM_ID),
                "--tag",
                TEST_TAG,
                "--weeks_ahead",
                "1",
            ],
        )
        main()
        text_out = capsys.readouterr().out
        assert "RECOMMENDATION:" in text_out

        monkeypatch.setattr(
            "sys.argv",
            [
                "airsenal_chip_report",
                "--fpl_team_id",
                str(TEST_FPL_TEAM_ID),
                "--tag",
                TEST_TAG,
                "--weeks_ahead",
                "1",
                "--json",
            ],
        )
        main()
        json_out = capsys.readouterr().out
        parsed = json.loads(json_out)
        assert parsed["fpl_team_id"] == TEST_FPL_TEAM_ID
    finally:
        _clear_season(db, CURRENT_SEASON, TEST_TAG, TEST_FPL_TEAM_ID, PLAYER_IDS)


def test_main_missing_squad_exits_with_error(monkeypatch, capsys):
    def _no_network(*_args, **_kwargs):
        msg = "no network access in tests"
        raise requests.exceptions.RequestException(msg)

    monkeypatch.setattr(fetcher, "get_current_summary_data", _no_network)
    monkeypatch.setattr(fetcher, "get_available_chips", _no_network)
    monkeypatch.setattr(fetcher, "get_current_picks", _no_network)

    monkeypatch.setattr(
        "sys.argv",
        [
            "airsenal_chip_report",
            "--fpl_team_id",
            "999999999",
            "--tag",
            "some_tag_that_wont_be_looked_up",
            "--weeks_ahead",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "ERROR" in capsys.readouterr().out
