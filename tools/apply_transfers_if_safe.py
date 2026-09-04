"""
Apply AIrsenal's latest transfer suggestions to a live FPL team, but only when
it is safe to do so with nobody watching.

This exists because `airsenal_run_pipeline --apply_transfers` is not safe to
run unattended as-is. It refuses to act - loudly, and without touching the
team - in three cases:

  1. No FPL credentials are configured. Nothing could be applied anyway, and
     failing clearly beats a half-completed run.
  2. The optimiser's plan for the upcoming gameweek involves a chip.
  3. There are no suggestions for the upcoming gameweek.

Case 2 is the important one. FPLDataFetcher.post_transfers documents that it
does *not* actually activate chips ("the transfers will be applied as normal
transfers with points hits"), and both that warning and the one in
make_transfers.check_proceed were written *after* the wildcard/freehit payload
flag was added - so the flag appears not to work in practice. Auto-applying an
11-player wildcard that fails to activate would land as 11 ordinary transfers
and cost roughly 30-40 points. Chip weeks are left for a human to do on the
website.

Exit codes:
  0 - transfers applied, or nothing needed doing
  2 - deliberately skipped; needs a human (chip week, or no credentials)
  1 - something went wrong
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from airsenal.framework.utils import CURRENT_SEASON, NEXT_GAMEWEEK, fetcher
from airsenal.scripts.make_transfers import (
    get_gw_transfer_suggestions,
    make_transfers,
)
from airsenal.scripts.set_lineup import set_lineup

SKIP_EXIT = 2


def _alert(alert_dir: Path, gameweek: int, team_id: int, reason: str) -> None:
    """Record a skipped week somewhere durable enough to be noticed later."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    alert_dir.mkdir(parents=True, exist_ok=True)
    path = alert_dir / f"NEEDS_ACTION_gw{gameweek}_team{team_id}_{stamp}.txt"
    path.write_text(
        f"AIrsenal auto-apply SKIPPED\n"
        f"  when:     {datetime.now(timezone.utc).isoformat()}\n"
        f"  team:     {team_id}\n"
        f"  gameweek: {gameweek}\n"
        f"  reason:   {reason}\n\n"
        f"Nothing was applied to the live team. Handle this gameweek manually\n"
        f"on the FPL website if you want the suggested changes.\n"
    )
    print(f"ALERT written to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply AIrsenal transfer suggestions if it is safe to do so"
    )
    parser.add_argument("--fpl_team_id", type=int, required=True)
    parser.add_argument(
        "--alert_dir",
        default="/root/airsenal_replay/apply_alerts",
        help="where to record gameweeks that were skipped and need a human",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="run every check and report what would happen, but never post",
    )
    args = parser.parse_args()

    team_id = args.fpl_team_id
    alert_dir = Path(args.alert_dir)
    print(f"=== auto-apply check: team {team_id}, GW{NEXT_GAMEWEEK}, {CURRENT_SEASON}")

    if not (fetcher.FPL_LOGIN and fetcher.FPL_PASSWORD):
        reason = (
            "No FPL_LOGIN/FPL_PASSWORD configured, so nothing can be posted to "
            "the FPL API."
        )
        print(f"SKIP: {reason}")
        _alert(alert_dir, NEXT_GAMEWEEK, team_id, reason)
        return SKIP_EXIT

    # Ask exactly what make_transfers would ask, so the guard can't diverge
    # from what would actually be applied.
    suggestions = get_gw_transfer_suggestions(team_id)
    if not suggestions:
        print(f"Nothing to do: no suggestions for GW{NEXT_GAMEWEEK}.")
        return 0

    transfer_player_ids, _, current_gw, chip_played = suggestions
    n_out, n_in = len(transfer_player_ids[0]), len(transfer_player_ids[1])
    print(f"suggestion: {n_out} out / {n_in} in, chip={chip_played or 'none'}")

    if chip_played:
        reason = (
            f"Strategy plays the '{chip_played}' chip in GW{current_gw} "
            f"({n_in} transfers). post_transfers does not reliably activate "
            f"chips, so applying this automatically risks taking those as "
            f"ordinary transfers (~{max(0, n_in - 1) * 4} pts of hits)."
        )
        print(f"SKIP: {reason}")
        _alert(alert_dir, current_gw, team_id, reason)
        return SKIP_EXIT

    if n_in == 0 and n_out == 0:
        print("Nothing to do: strategy suggests no transfers this gameweek.")
    elif args.dry_run:
        print(f"DRY RUN: would apply {n_in} transfer(s) to team {team_id}.")
    else:
        print(f"Applying {n_in} transfer(s) to team {team_id}...")
        if make_transfers(team_id, skip_check=True) is False:
            print("ERROR: make_transfers declined to apply.")
            return 1
        print("Transfers applied.")

    if args.dry_run:
        print(f"DRY RUN: would set lineup/captain for team {team_id}.")
    else:
        print("Setting lineup and captain...")
        set_lineup(team_id, skip_check=True)
        print("Lineup set.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
