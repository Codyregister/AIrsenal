"""
Price change predictor.

FPL prices rise or fall at a daily deadline (midnight UK time) based on
transfer activity, using an algorithm FPL has never published (their own
new "Price Change Prediction" page, introduced for 2026/27, is explicit that
it's "a guide", not a guarantee). This module estimates a rough "momentum"
signal from publicly-visible data (net transfers since yesterday's snapshot,
relative to a player's ownership) - it is NOT a calibrated rise/fall
classifier.

Why not a classifier: backtested a simple ownership-relative threshold
against real historical data (PlayerAttributes' weekly price/transfer
snapshots, ~117k player-gameweek transitions across 5 seasons) and found
weak separation - at any threshold tried, precision for predicting an actual
rise or fall was in the single-to-low-double digits (see git history for the
exact numbers). That backtest used weekly-granularity data as a proxy for a
daily signal, which likely understates how well daily data (this module's
actual intended input, via PriceChangeSnapshot) would perform - but until
enough real daily history accumulates to re-validate properly, presenting a
"likely/very likely" style verdict would overclaim confidence the evidence
doesn't support. Instead, this ranks players by momentum and leaves the
interpretation to the reader, same spirit as FPL's own "guide, not
guarantee" framing.
"""

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm.session import Session

from airsenal.framework.schema import PriceChangeSnapshot, session
from airsenal.framework.season import CURRENT_SEASON


@dataclass
class PriceMomentum:
    player_id: int
    price: int  # tenths of £m, as returned by the FPL API
    net_transfers_today: int  # positive = more IN than OUT since yesterday
    selected_by_percent: float
    momentum_pct: float  # net_transfers_today as a % of current owners
    snapshot_date: str
    days_of_history: int  # how many daily snapshots this is based on


def get_price_momentum(
    season: str = CURRENT_SEASON,
    dbsession: Session = session,
) -> list[PriceMomentum]:
    """
    For every player with at least two PriceChangeSnapshot rows this season,
    compute today's net transfer delta relative to the most recent prior
    snapshot, and a momentum score (that delta as a % of current owners).

    Returns an empty list for players/situations with fewer than two
    snapshots (nothing to diff against yet) - this is expected for the
    first day or two after the cron starts running, and isn't an error.
    """
    rows = dbsession.scalars(
        select(PriceChangeSnapshot)
        .where(PriceChangeSnapshot.season == season)
        .order_by(PriceChangeSnapshot.player_id, PriceChangeSnapshot.snapshot_date)
    ).all()
    if not rows:
        return []

    df = pd.DataFrame(
        [
            {
                "player_id": r.player_id,
                "snapshot_date": r.snapshot_date,
                "price": r.price,
                "transfers_in": r.transfers_in,
                "transfers_out": r.transfers_out,
                "selected_by_percent": r.selected_by_percent,
            }
            for r in rows
        ]
    )
    df["balance"] = df["transfers_in"] - df["transfers_out"]

    results = []
    for player_id, group in df.groupby("player_id"):
        g = group.sort_values("snapshot_date")
        if len(g) < 2:
            continue
        latest = g.iloc[-1]
        prev = g.iloc[-2]

        net_transfers_today = int(latest["balance"] - prev["balance"])
        # selected_by_percent is a % of all managers - use the prior day's
        # value (owners going into today) as the base for the momentum %,
        # matching how FPL's own progress-towards-threshold framing works
        # (relative to who already owns the player, not who might buy it).
        owners_pct = max(prev["selected_by_percent"], 0.01)
        momentum_pct = 100 * net_transfers_today / (owners_pct * 1000)

        results.append(
            PriceMomentum(
                player_id=int(player_id),
                price=int(latest["price"]),
                net_transfers_today=net_transfers_today,
                selected_by_percent=float(latest["selected_by_percent"]),
                momentum_pct=round(momentum_pct, 3),
                snapshot_date=str(latest["snapshot_date"]),
                days_of_history=len(g),
            )
        )

    return sorted(results, key=lambda r: r.momentum_pct, reverse=True)


def status_label(momentum_pct: float) -> str:
    """
    Coarse, deliberately non-committal label for a momentum score - "rising"/
    "falling" momentum, not "will rise"/"will fall". See module docstring for
    why this doesn't attempt a calibrated likely/very likely verdict.
    """
    if momentum_pct > 5:
        return "strong rising momentum"
    if momentum_pct > 1:
        return "rising momentum"
    if momentum_pct < -5:
        return "strong falling momentum"
    if momentum_pct < -1:
        return "falling momentum"
    return "little momentum"
