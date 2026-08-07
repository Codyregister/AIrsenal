"""
Fetch a daily snapshot of price/transfer-activity data for every player, for
the price-change predictor (see airsenal/framework/price_change.py).

FPL prices change at a daily deadline (midnight UK time) based on transfer
activity - a much finer cadence than PlayerAttributes' one-row-per-gameweek
snapshot can represent, so this writes to its own table
(PriceChangeSnapshot), upserting on (player_id, season, today's date) so
re-running within the same day just refreshes today's row rather than
creating duplicates.

Meant to be run several times a day (see tools/price_change_snapshot_cron.sh)
- not part of airsenal_update_db/airsenal_run_pipeline, since those are
run on-demand rather than on a tight daily/hourly schedule.
"""

import datetime

from sqlalchemy import select
from sqlalchemy.orm.session import Session

from airsenal.framework.data_fetcher import FPLDataFetcher
from airsenal.framework.schema import PriceChangeSnapshot, session, session_scope
from airsenal.framework.season import CURRENT_SEASON
from airsenal.framework.utils import get_player_from_api_id


def fill_price_change_snapshot(
    season: str = CURRENT_SEASON,
    fetcher: FPLDataFetcher | None = None,
    dbsession: Session = session,
) -> int:
    """
    Fetch current bootstrap-static data and upsert today's PriceChangeSnapshot
    row for every player found. Returns the number of players updated.
    """
    if fetcher is None:
        fetcher = FPLDataFetcher()

    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat()

    player_data = fetcher.get_player_summary_data()
    n_updated = 0
    for api_id, p_summary in player_data.items():
        player = get_player_from_api_id(api_id, dbsession=dbsession)
        if not player:
            continue

        existing = dbsession.scalars(
            select(PriceChangeSnapshot).where(
                PriceChangeSnapshot.player_id == player.player_id,
                PriceChangeSnapshot.season == season,
                PriceChangeSnapshot.snapshot_date == today,
            )
        ).first()
        snap = existing or PriceChangeSnapshot()
        snap.player_id = player.player_id
        snap.season = season
        snap.snapshot_date = today
        snap.price = int(p_summary["now_cost"])
        snap.transfers_in = int(p_summary["transfers_in"])
        snap.transfers_out = int(p_summary["transfers_out"])
        snap.selected_by_percent = float(p_summary["selected_by_percent"])
        snap.timestamp = now
        if not existing:
            dbsession.add(snap)
        n_updated += 1

    dbsession.commit()
    return n_updated


def main():
    with session_scope() as dbsession:
        n = fill_price_change_snapshot(dbsession=dbsession)
    print(f"PRICE CHANGE SNAPSHOT: updated {n} players for {datetime.date.today()}")


if __name__ == "__main__":
    main()
