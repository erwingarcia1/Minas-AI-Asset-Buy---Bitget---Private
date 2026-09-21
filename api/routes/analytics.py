"""Advanced analytics: performance vs live prices, benchmarks, recurring-spend
projections, and 30-day volatility per coin. Price lookups degrade
gracefully — with the market data source down, responses carry cost-basis
figures and price_available=false instead of failing."""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import User
from api.routes.user import get_current_user
import api.services.market_data as market_mod
from api.services import bitget_market

router = APIRouter()

BUYS_PER_MONTH = {"daily": 30.44, "weekly": 4.35, "biweekly": 2.17, "monthly": 1.0, "one_time": 0.0}
ESTIMATES_NOTE = ("Estimates only, not financial advice; past performance doesn't guarantee "
                  "future results.")


class HoldingPerformance(BaseModel):
    crypto: str
    total_amount: float
    invested_usdt: float
    average_buy_price: float
    price_precision: int = 8           # decimals Bitget quotes the pair in; show prices with exactly these
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pnl_usdt: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None


class PerformanceResponse(BaseModel):
    """Per-holding figures only. No ranking (best/worst) is produced: every
    percentage is in the table and ordering them adds judgement, not fact."""
    price_available: bool
    holdings: List[HoldingPerformance]
    total_invested_usdt: float
    total_market_value: Optional[float] = None
    total_pnl_usdt: Optional[float] = None
    total_pnl_pct: Optional[float] = None


async def _holdings(db, user_id):
    """[(crypto, amount, invested_usd)] from successful Auto-Buy buys (purchase_transactions)."""
    from api.services import history
    return await history.holdings(db, user_id)


@router.get("/performance", response_model=PerformanceResponse)
async def performance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unrealized P&L per holding against live prices."""
    holdings = []
    any_price = False
    for crypto, amount, invested in await _holdings(db, current_user.id):
        price = await market_mod.market_data.get_price(crypto)
        h = HoldingPerformance(
            crypto=crypto, total_amount=round(amount, 8),
            invested_usdt=round(invested, 2),
            average_buy_price=round(invested / amount, 8),
            price_precision=await bitget_market.price_precision_async(crypto))
        if price is not None:
            any_price = True
            h.current_price = price
            h.market_value = round(amount * price, 2)
            h.unrealized_pnl_usdt = round(h.market_value - invested, 2)
            h.unrealized_pnl_pct = round(h.unrealized_pnl_usdt / invested * 100, 2)
        holdings.append(h)

    total_invested = round(sum(h.invested_usdt for h in holdings), 2)
    resp = PerformanceResponse(price_available=any_price, holdings=holdings,
                               total_invested_usdt=total_invested)
    priced = [h for h in holdings if h.market_value is not None]
    if priced:
        resp.total_market_value = round(sum(h.market_value for h in priced), 2)
        invested_priced = sum(h.invested_usdt for h in priced)
        resp.total_pnl_usdt = round(resp.total_market_value - invested_priced, 2)
        resp.total_pnl_pct = round(resp.total_pnl_usdt / invested_priced * 100, 2)
    return resp


class ComparisonEntry(BaseModel):
    crypto: str
    average_buy_price: float
    price_precision: int = 8
    current_price: Optional[float] = None
    buy_price_vs_market_pct: Optional[float] = None  # negative = bought below market


class InvestedShare(BaseModel):
    crypto: str
    share_pct: float   # this coin's share of the total COST invested (not of current value)


class ComparisonResponse(BaseModel):
    """Average buy price vs the market, and each coin's share of invested
    cost. No score is derived from the shares: the shares are the fact."""
    entries: List[ComparisonEntry]
    share_of_invested: List[InvestedShare]   # by cost, biggest first, sums to 100


@router.get("/comparison", response_model=ComparisonResponse)
async def comparison(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Average buy price vs the market now, and each coin's share of the
    total cost invested."""
    holdings = await _holdings(db, current_user.id)
    entries = []
    for crypto, amount, invested in holdings:
        avg = invested / amount
        price = await market_mod.market_data.get_price(crypto)
        e = ComparisonEntry(crypto=crypto, average_buy_price=round(avg, 8),
                            price_precision=await bitget_market.price_precision_async(crypto))
        if price is not None:
            e.current_price = price
            e.buy_price_vs_market_pct = round((avg - price) / price * 100, 2)
        entries.append(e)
    total = sum(inv for _, _, inv in holdings)
    shares = sorted((InvestedShare(crypto=c, share_pct=round(inv / total * 100, 1)) for c, _, inv in holdings),
                    key=lambda x: -x.share_pct) if total else []
    return ComparisonResponse(entries=entries, share_of_invested=shares)


class RecurringProjection(BaseModel):
    crypto: str
    frequency: str
    amount_usdt: float
    monthly_usdt: float
    purchase_type: str = "autobuy"   # autobuy | dca


class RecurringResponse(BaseModel):
    schedules: List[RecurringProjection]
    expected_monthly_usdt: float
    projected_annual_usdt: float
    note: str = ESTIMATES_NOTE


@router.get("/recurring", response_model=RecurringResponse)
async def recurring(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Spend projection from the user's active Auto-Buy purchases (both
    ``autobuy`` and ``dca`` types). One-time purchases contribute nothing to
    the recurring monthly figure. Estimates only."""
    from api.models import Purchase
    purchases = (await db.execute(
        select(Purchase).where(
            Purchase.user_id == current_user.id,
            Purchase.status == "active"))).scalars().all()
    rows = [RecurringProjection(
        crypto=p.crypto_symbol, frequency=p.frequency, amount_usdt=p.amount_usd,
        monthly_usdt=round(p.amount_usd * BUYS_PER_MONTH.get(p.frequency, 0.0), 2),
        purchase_type=p.purchase_type)
        for p in purchases]
    monthly = round(sum(r.monthly_usdt for r in rows), 2)
    return RecurringResponse(schedules=rows, expected_monthly_usdt=monthly,
                             projected_annual_usdt=round(monthly * 12, 2))


class VolatilityEntry(BaseModel):
    crypto: str
    volatility_30d_pct: Optional[float] = None
    level: str  # low | moderate | high | unknown
    summary: str   # the metric in words, e.g. "30-day volatility 3.1% (moderate)." — no guidance


class VolatilityResponse(BaseModel):
    entries: List[VolatilityEntry]


@router.get("/volatility", response_model=VolatilityResponse)
async def volatility(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """30-day daily-return volatility per held or scheduled coin, bucketed
    low (< 2%) / moderate (2–5%) / high (>= 5%). A description of the
    metric only; no cadence or sizing guidance is produced."""
    from api.models import Purchase
    cryptos = {c for c, _, _ in await _holdings(db, current_user.id)}
    # scheduled coins: active Auto-Buy purchases (both types)
    cryptos |= {p.crypto_symbol for p in (await db.execute(
        select(Purchase).where(
            Purchase.user_id == current_user.id,
            Purchase.status == "active"))).scalars().all()}
    entries = []
    for crypto in sorted(cryptos):
        vol = await market_mod.market_data.get_volatility_30d(crypto)
        if vol is None:
            entries.append(VolatilityEntry(crypto=crypto, level="unknown",
                                           summary="Volatility data unavailable right now."))
            continue
        level = "high" if vol >= 5 else "moderate" if vol >= 2 else "low"
        entries.append(VolatilityEntry(crypto=crypto, volatility_30d_pct=vol, level=level,
                                       summary=f"30-day volatility {vol}% ({level})."))
    return VolatilityResponse(entries=entries)
