"""
Script to replay all or part of a season, to allow evaluation of different
code and strategies.
"""

import argparse
import json
import warnings
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm.session import Session
from tqdm import TqdmWarning, tqdm

from airsenal.framework.bpl_interface import DEFAULT_TEAM_EPSILON
from airsenal.framework.multiprocessing_utils import set_multiprocessing_start_method
from airsenal.framework.optimization_utils import get_starting_squad
from airsenal.framework.schema import Transaction, session_scope
from airsenal.framework.utils import (
    get_gameweeks_array,
    get_max_gameweek,
    get_player_name,
    parse_team_model_from_str,
)
from airsenal.scripts.fill_predictedscore_table import make_predictedscore_table
from airsenal.scripts.fill_transfersuggestion_table import (
    CHIP_STRATEGIES,
    run_optimization,
)
from airsenal.scripts.squad_builder import fill_initial_squad


def get_dummy_id(season: str, dbsession: Session) -> int:
    team_ids = dbsession.scalars(
        select(Transaction.fpl_team_id).where(Transaction.season == season).distinct()
    ).all()
    if not team_ids or min(team_ids) > 0:
        return -1
    return min(team_ids) - 1


def print_replay_params(
    season: str,
    gameweek_start: int,
    gameweek_end: int,
    tag_prefix: str,
    fpl_team_id: int,
) -> None:
    print("=" * 30)
    print(f"Replay {season} season from GW{gameweek_start} to GW{gameweek_end}")
    print(f"tag_prefix = {tag_prefix}")
    print(f"fpl_team_id = {fpl_team_id}")
    print("=" * 30)


def replay_season(
    season: str,
    gameweek_start: int = 1,
    gameweek_end: int | None = None,
    new_squad: bool = True,
    weeks_ahead: int = 3,
    num_thread: int = 4,
    transfers: bool = True,
    tag_prefix: str = "",
    team_model: str = "extended",
    team_model_args: dict | None = None,
    fpl_team_id: int | None = None,
    max_opt_transfers: int = 2,
    chip_strategy: str = "off",
    chip_gameweeks: dict | None = None,
    risk_lambda: float = 0.8,
    num_iterations: int = 100,
) -> dict[str, str | int | float | list | dict]:
    """Replay (all or part of) a past season, optimising transfers (and,
    depending on ``chip_strategy``, chips) gameweek by gameweek, and write
    the results to ``{tag_prefix}.json``.

    chip_strategy / chip_gameweeks / risk_lambda are passed straight through
    to ``run_optimization`` every gameweek (see docs/chip_timing_spec.md
    §4.4's validation plan):

    - "off" (default): chips are never played - bit-identical to AIrsenal's
      pre-chip-timing behaviour.
    - "manual": chips are only played according to ``chip_gameweeks`` (e.g.
      all four chips set to 0, "any week", reproduces the greedy
      any-week-is-fine behaviour that predates this feature).
    - "auto": ``chip_timing.recommend_chip_timing`` decides when (if at all)
      to play each chip, tuned by ``risk_lambda``.

    num_iterations controls the population/generation size of the DEAP GA
    used both for the initial squad build (fill_initial_squad) and for any
    wildcard/free-hit candidate squad rebuilt while searching the transfer
    strategy tree (run_optimization -> make_new_squad). It defaults to 100
    to match production, but validation replays - especially with
    chip_strategy values that let the tree search consider wildcard/free
    hit in many candidate gameweeks (e.g. "manual" with all *_week=0) -
    can multiply that cost by dozens of GA reruns per gameweek; passing a
    smaller value (e.g. 15-20) trades optimisation precision for tractable
    replay wall-clock time. See docs/chip_timing_spec.md §4.4.

    Returns the ``replay_results`` dict that's also written to
    ``{tag_prefix}.json`` (including ``total_actual_points`` /
    ``total_expected_points`` season totals, and - per-gameweek - which
    chip, if any, was played), so callers (e.g. a validation script running
    several configurations back to back) don't have to re-read the file.
    """
    if team_model_args is None:
        team_model_args = {"epsilon": DEFAULT_TEAM_EPSILON}
    if chip_gameweeks is None:
        chip_gameweeks = {}
    start = datetime.now()
    if gameweek_end is None:
        gameweek_end = get_max_gameweek(season)
    if fpl_team_id is None:
        with session_scope() as session:
            fpl_team_id = get_dummy_id(season, dbsession=session)
    if not tag_prefix:
        start_str = start.strftime("%Y%m%d%H%M")
        tag_prefix = (
            f"Replay_{season}_GW{gameweek_start}_GW{gameweek_end}_"
            f"{start_str}_{team_model}"
        )
    print_replay_params(season, gameweek_start, gameweek_end, tag_prefix, fpl_team_id)

    team_model_class = parse_team_model_from_str(team_model)

    # store results in a dictionary, which we will later save to a json file
    replay_results: dict[str, str | int | float | list | dict] = {}
    replay_results["tag"] = tag_prefix
    replay_results["season"] = season
    replay_results["weeks_ahead"] = weeks_ahead
    replay_results["chip_strategy"] = chip_strategy
    replay_results["chip_gameweeks"] = dict(chip_gameweeks)
    replay_results["risk_lambda"] = risk_lambda
    replay_results["gameweeks"] = []
    replay_range = range(gameweek_start, gameweek_end + 1)
    for idx, gw in enumerate(tqdm(replay_range, desc="REPLAY PROGRESS")):
        print(f"GW{gw} ({idx + 1} out of {len(replay_range)})...")
        with session_scope() as session:
            gw_range = get_gameweeks_array(
                weeks_ahead, gameweek_start=gw, season=season, dbsession=session
            )
            tag = make_predictedscore_table(
                gw_range=gw_range,
                season=season,
                tag_prefix=tag_prefix,
                team_model=team_model_class,
                team_model_args=team_model_args,
                dbsession=session,
            )
        gw_result = {
            "gameweek": gw,
            "predictions_tag": tag,
            "optimization_fallback": False,
        }

        if not transfers:
            continue
        if gw == gameweek_start and new_squad:
            print("Creating initial squad...")
            squad = fill_initial_squad(
                tag,
                gw_range,
                season,
                fpl_team_id,
                num_generations=num_iterations,
                population_size=num_iterations,
                is_replay=True,
            )
            # no points hits due to unlimited transfers to initialise team
            best_strategy: dict[str, dict[str, int | list[int]]] | None = {
                "points_hit": {str(gw): 0},
                "free_transfers": {str(gw): 0},
                "num_transfers": {str(gw): 0},
                "players_in": {str(gw): []},
                "players_out": {str(gw): []},
            }
        else:
            print("Optimising transfers...")
            # find best squad and the strategy for this gameweek
            try:
                squad, best_strategy = run_optimization(
                    gw_range,
                    tag,
                    season=season,
                    fpl_team_id=fpl_team_id,
                    num_thread=num_thread,
                    is_replay=True,
                    max_opt_transfers=max_opt_transfers,
                    num_iterations=num_iterations,
                    chip_gameweeks=dict(chip_gameweeks),
                    chip_strategy=chip_strategy,
                    risk_lambda=risk_lambda,
                )
            except ValueError as exc:
                # The tree search found no valid strategy at all for this
                # gameweek - can genuinely happen on/near a blank gameweek,
                # where a wildcard/free-hit candidate squad rebuild fails for
                # every option in the (small, weeks_ahead-limited) search
                # tree due to too few eligible players in some position (see
                # SquadOpt._check_positions_available). Rather than aborting
                # the whole season's replay over one unplayable gameweek,
                # fall back to what a human manager would actually do here:
                # make no transfer and keep the existing squad. No
                # Transaction row is written, which is exactly correct for
                # "nothing changed this gameweek" (next gameweek's starting
                # squad is reconstructed from the Transaction table).
                print(
                    f"WARNING: optimization failed for GW{gw} ({exc}) - "
                    "falling back to no transfers this gameweek rather than "
                    "aborting the replay."
                )
                gw_result["optimization_fallback"] = True
                squad = get_starting_squad(
                    next_gw=gw, season=season, fpl_team_id=fpl_team_id, use_api=False
                )
                best_strategy = {
                    "points_hit": {str(gw): 0},
                    "free_transfers": {str(gw): 0},
                    "num_transfers": {str(gw): 0},
                    "players_in": {str(gw): []},
                    "players_out": {str(gw): []},
                }
        if best_strategy is None:
            msg = f"Failed to find a strategy for GW{gw}!"
            raise ValueError(msg)

        gw_result["starting_11"] = []
        gw_result["subs"] = []
        for p in squad.players:
            if p.is_starting:
                gw_result["starting_11"].append(p.name)
            else:
                gw_result["subs"].append(p.name)
            if p.is_captain:
                gw_result["captain"] = p.name
            elif p.is_vice_captain:
                gw_result["vice_captain"] = p.name
        # obtain information about the strategy used for gameweek
        gw_result["free_transfers"] = best_strategy["free_transfers"][str(gw)]
        gw_result["num_transfers"] = best_strategy["num_transfers"][str(gw)]
        gw_result["points_hit"] = best_strategy["points_hit"][str(gw)]
        # Which chip, if any, was actually committed to this gameweek (as
        # opposed to merely considered somewhere later in this week's
        # look-ahead horizon - only the current gameweek's part of the
        # strategy is ever actually "applied", matching fill_transaction_table's
        # fill_gw = min(strat_gws) semantics above).
        chip_played_this_gw = best_strategy.get("chips_played", {}).get(str(gw))
        gw_result["chip_played"] = chip_played_this_gw
        if chip_played_this_gw is not None:
            # A chip can only be played once per season. run_optimization has
            # no season-level memory of chips played in earlier weeks of this
            # replay (chip_strategy="auto" infers it from TransferSuggestion
            # rows in the DB, but chip_strategy="off"/"manual" - i.e. the
            # static chip_gameweeks passed in below - does not), so once a
            # chip has actually been played, stop offering it again for the
            # rest of this replay.
            chip_gameweeks[chip_played_this_gw] = -1
        players_in = best_strategy["players_in"][str(gw)]
        players_out = best_strategy["players_out"][str(gw)]
        if not isinstance(players_in, list) or not isinstance(players_out, list):
            msg = (
                "players_in and players_out should be lists of player IDs, "
                f"got {type(players_in)} and {type(players_out)}"
            )
            raise TypeError(msg)
        gw_result["players_in"] = [get_player_name(p) for p in players_in]
        gw_result["players_out"] = [get_player_name(p) for p in players_out]
        # compute expected and actual points for gameweek
        hit_points = gw_result["points_hit"]
        if not isinstance(hit_points, int):
            msg = (
                f"points_hit should be an integer, got {type(hit_points)}: {hit_points}"
            )
            raise TypeError(msg)
        gw_result["points_hit"] = hit_points
        # expected points minus points hit gives the expected points for the gameweek
        exp_points = squad.get_expected_points(gw, tag)
        gw_result["expected_points"] = exp_points
        actual_points = squad.get_actual_points(gw, season)
        gw_result["actual_points"] = actual_points - gw_result["points_hit"]
        if not isinstance(replay_results["gameweeks"], list):
            msg = (
                f"replay_results['gameweeks'] should be a list, "
                f"got {type(replay_results['gameweeks'])}"
            )
            raise TypeError(msg)
        replay_results["gameweeks"].append(gw_result)
        print("-" * 30)

    end = datetime.now()
    elapsed = end - start
    replay_results["elapsed"] = elapsed.total_seconds()
    gw_results = replay_results["gameweeks"]
    if not isinstance(gw_results, list):
        msg = f"replay_results['gameweeks'] should be a list, got {type(gw_results)}"
        raise TypeError(msg)
    replay_results["total_actual_points"] = sum(
        gwr["actual_points"] for gwr in gw_results if "actual_points" in gwr
    )
    replay_results["total_expected_points"] = sum(
        gwr["expected_points"] for gwr in gw_results if "expected_points" in gwr
    )
    replay_results["chips_played"] = {
        gwr["gameweek"]: gwr["chip_played"]
        for gwr in gw_results
        if gwr.get("chip_played") is not None
    }
    with open(f"{tag_prefix}.json", "w") as outfile:
        json.dump(replay_results, outfile)
    print_replay_params(season, gameweek_start, gameweek_end, tag_prefix, fpl_team_id)
    print(
        f"Total actual points: {replay_results['total_actual_points']}, "
        f"total expected points: {replay_results['total_expected_points']:.1f}, "
        f"chips played: {replay_results['chips_played']}"
    )
    print("DONE!")
    return replay_results


def main():
    """
    replay a particular FPL season
    """
    parser = argparse.ArgumentParser(description="replay a particular FPL season")

    parser.add_argument(
        "--gameweek_start", help="first gameweek to look at", type=int, default=1
    )
    parser.add_argument(
        "--gameweek_end", help="last gameweek to look at", type=int, default=None
    )
    parser.add_argument(
        "--weeks_ahead", help="how many weeks ahead to fill", type=int, default=3
    )
    parser.add_argument(
        "--season", help="season, in format e.g. '1819'", type=str, required=True
    )
    parser.add_argument(
        "--fpl_team_id",
        help="FPL team ID (defaults to a unique, negative value)",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--resume",
        help=(
            "If set, use a pre-existing squad and transactions in the database "
            "for this team ID as the starting point, rather than creating a new squad. "
            "fpl_team_id must be defined."
        ),
        action="store_true",
    )
    parser.add_argument(
        "--num_thread",
        help="number of threads to parallelise over",
        type=int,
        default=4,
    )
    parser.add_argument(
        "--loop",
        help="How many times to repeat repla (default 1, -1 to loop continuously)",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--team_model",
        help="Specify name of the team model.",
        type=str,
        default="extended",
        choices=["extended", "random"],
    )
    parser.add_argument(
        "--epsilon",
        help="how much to downweight games by in exponential time weighting",
        type=float,
        default=DEFAULT_TEAM_EPSILON,
    )
    parser.add_argument(
        "--max_transfers",
        help=(
            "maximum number of transfers to consider each gameweek [EXPERIMENTAL: "
            "increasing this value above 2 may make the optimisation very slow!]"
        ),
        type=int,
        default=2,
    )
    parser.add_argument(
        "--chip_strategy",
        help=(
            "How to decide when to play chips, when none of the --*_week "
            "options below have been set (an explicit --*_week option always "
            "overrides this). 'off' (default): never play a chip - matches "
            "AIrsenal's pre-chip-timing behaviour. 'manual': same as 'off' - "
            "chips are only played via the --*_week options (e.g. all four "
            "set to 0, 'any week', reproduces the historic greedy "
            "any-week-is-fine behaviour). 'auto': use "
            "airsenal.framework.chip_timing.recommend_chip_timing to decide "
            "whether to play each available chip within the optimisation "
            "horizon. See docs/chip_timing_spec.md §4.3/§4.4."
        ),
        choices=CHIP_STRATEGIES,
        default="off",
    )
    parser.add_argument(
        "--wildcard_week",
        help="play wildcard in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--free_hit_week",
        help="play free hit in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--triple_captain_week",
        help="play triple captain in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--bench_boost_week",
        help="play bench_boost in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--risk_lambda",
        help=(
            "Only used when --chip_strategy=auto: how much to discount "
            "future chip value relative to playing now. See "
            "docs/chip_timing_spec.md §4.1/§4.4."
        ),
        type=float,
        default=0.8,
    )
    parser.add_argument(
        "--num_iterations",
        help=(
            "Population/generation size for the DEAP GA used for the "
            "initial squad build and any wildcard/free-hit candidate "
            "squads considered during the transfer-strategy search. "
            "Defaults to 100 (production default); lower values (e.g. "
            "15-20) trade optimisation precision for much faster replay "
            "wall-clock time - useful when replaying with chip_strategy "
            "values that let the search consider wildcard/free hit in "
            "many candidate gameweeks."
        ),
        type=int,
        default=100,
    )

    args = parser.parse_args()
    if args.resume and not args.fpl_team_id:
        msg = "fpl_team_id must be set to use the resume argument"
        raise RuntimeError(msg)

    set_multiprocessing_start_method()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", TqdmWarning)
        n_completed = 0
        while (args.loop == -1) or (n_completed < args.loop):
            print("*" * 15)
            print(f"RUNNING REPLAY {n_completed + 1}")
            print("*" * 15)
            replay_season(
                season=args.season,
                gameweek_start=args.gameweek_start,
                gameweek_end=args.gameweek_end,
                new_squad=not args.resume,
                weeks_ahead=args.weeks_ahead,
                num_thread=args.num_thread,
                fpl_team_id=args.fpl_team_id,
                team_model=args.team_model,
                team_model_args={"epsilon": args.epsilon},
                max_opt_transfers=args.max_transfers,
                chip_strategy=args.chip_strategy,
                chip_gameweeks={
                    "wildcard": args.wildcard_week,
                    "free_hit": args.free_hit_week,
                    "triple_captain": args.triple_captain_week,
                    "bench_boost": args.bench_boost_week,
                },
                risk_lambda=args.risk_lambda,
                num_iterations=args.num_iterations,
            )
            n_completed += 1


if __name__ == "__main__":
    main()
