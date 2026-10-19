"""Portfolio value and amount invested over time for one customer.

For each bucket end t:
  holdings(t) = net acquired quantity (history.net_of_fee) of every successful
                buy executed at or before t; the same "acquired" figure the
                Holdings table, the performance card and the allocation donut
                use. Coins withdrawn to the customer's wallet are still owned,
                so withdrawals do not change holdings(t).
  invested(t) = sum of cost_usd of those buys: a step that rises at a buy and
                never slopes between buys.
  value(t)    = sum over coins of holdings(t) x Bitget candle close for the
                bucket ending at t; a bucket with no candle carries the
                previous close forward (candles.closes_for).

The final point is "now", priced with the same current price the performance
card uses (market_data.get_price) and summed the same way (each coin's market
value rounded to cents, then added), so the chart's last value, the donut's
total and the performance card's market value agree by construction; only the
historical points come from Bitget candles. The seam between the last candle
and that price is left as it is: no smoothing.

change_usdt is the change in value over the range EXCLUDING what buys made in
the range added: current_value - start_value - (cost of buys after the start).
change_pct divides that by start_value + that cost. It is a simple return, not
time-weighted; it says how the coins held moved, not how well the timing was.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Purchase, PurchaseTransaction
from api.services import candles, history
from api.services import market_data as market_mod

NOTE = "Estimates only, not financial advice. Values use Bitget candle closes per bucket and the current price for the last point."


@dataclass
class Buy:
    t: float
    crypto: str
    amount: float          # net acquired (after the coin-denominated trading fee)
    cost_usd: float


@dataclass
class Series:
    range: str
    granularity: str
    bucket_seconds: int
    points: List[dict] = field(default_factory=list)   # {t, value, invested}
    buys: List[dict] = field(default_factory=list)     # {t, crypto, amount, cost_usd}
    current_value: Optional[float] = None
    invested: float = 0.0
    change_usdt: Optional[float] = None    # value change over the range, excluding buys made in the range
    change_pct: Optional[float] = None
    carried_forward: int = 0      # bucket closes carried forward over a missing candle (all coins)
    unpriced: List[str] = field(default_factory=list)  # coins with no current price (left out of the final point)
    note: str = NOTE


async def load_buys(db: AsyncSession, user_id) -> List[Buy]:
    rows = (await db.execute(
        select(PurchaseTransaction, Purchase.crypto_symbol, Purchase.amount_usd)
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == history.DB_SUCCESS,
               PurchaseTransaction.tx_type.in_(history.BUY_TYPES))
        .order_by(PurchaseTransaction.executed_at, PurchaseTransaction.created_at))).all()
    buys: List[Buy] = []
    for tx, coin, amount_usd in rows:
        when = (tx.executed_at or tx.created_at).timestamp()
        cost = float(tx.cost_usd) if tx.cost_usd is not None else float(amount_usd)   # same fallback as history.positions
        buys.append(Buy(when, coin, history.net_of_fee(tx, coin), cost))
    return buys


def holdings_at(t: float, buys: List[Buy]) -> Dict[str, float]:
    h: Dict[str, float] = {}
    for b in buys:
        if b.t <= t:
            h[b.crypto] = h.get(b.crypto, 0.0) + b.amount
    return h


def invested_at(t: float, buys: List[Buy]) -> float:
    return sum(b.cost_usd for b in buys if b.t <= t)


def bucket_ends_for(range_key: str, now: float, first_buy: Optional[float]) -> tuple:
    """(granularity, step, [closed bucket ends, oldest first]) for a range.
    The last end is the end of the last CLOSED bucket at `now`; "All" runs
    from the bucket of the first buy, capped at candles.ALL_MAX_DAYS."""
    granularity, step, count = candles.RANGES[range_key]
    last = candles.bucket_end(now, step) - step
    if count is None:
        if first_buy is None:
            return granularity, step, []
        count = min(candles.ALL_MAX_DAYS, int((last - (candles.bucket_end(first_buy, step) - step)) // step) + 1)
    ends = [last - i * step for i in range(count - 1, -1, -1)]
    return granularity, step, ends


async def build(db: AsyncSession, user_id, range_key: str, now: Optional[float] = None) -> Series:
    now = now or time.time()
    buys = await load_buys(db, user_id)
    first_buy = buys[0].t if buys else None
    granularity, step, ends = bucket_ends_for(range_key, now, first_buy)
    series = Series(range=range_key, granularity=granularity, bucket_seconds=step)
    if not buys:
        return series
    coins = sorted({b.crypto for b in buys})
    closes: Dict[str, Dict[int, Optional[float]]] = {}
    for coin in coins:
        # only the buckets from this coin's first buy on need a price
        needed = [e for e in ends if e >= min(b.t for b in buys if b.crypto == coin)]
        closes[coin], carried = await asyncio.to_thread(candles.closes_for, f"{coin}USDT", granularity, step, needed)
        series.carried_forward += len(carried)
    for end in ends:
        value = 0.0
        for coin, qty in holdings_at(end, buys).items():
            close = closes[coin].get(end)
            if close is not None:          # None only before the pair's first candle
                value += qty * close
        series.points.append({"t": end, "value": round(value, 2), "invested": round(invested_at(end, buys), 2)})
    # the final point: now, at the current price the performance card uses,
    # summed as the card sums it (per-coin market value rounded to cents)
    current = 0.0
    priced_any = False
    for coin, qty in holdings_at(now, buys).items():
        price = await market_mod.market_data.get_price(coin)
        if price is None:
            series.unpriced.append(coin)
            continue
        priced_any = True
        current += round(qty * price, 2)
    series.invested = round(invested_at(now, buys), 2)
    if priced_any:
        series.current_value = round(current, 2)
        series.points.append({"t": int(now), "value": series.current_value, "invested": series.invested})
    window_start = ends[0] - step if ends else now
    series.buys = [{"t": int(b.t), "crypto": b.crypto, "amount": b.amount, "cost_usd": round(b.cost_usd, 2)}
                   for b in buys if b.t >= window_start]
    # change over the range, excluding what buys made in the range added (see module doc)
    if series.current_value is not None and len(series.points) > 1:
        start = series.points[0]
        added = series.invested - start["invested"]
        base = start["value"] + added
        series.change_usdt = round(series.current_value - start["value"] - added, 2)
        series.change_pct = round(series.change_usdt / base * 100, 2) if base > 0 else None
    return series
