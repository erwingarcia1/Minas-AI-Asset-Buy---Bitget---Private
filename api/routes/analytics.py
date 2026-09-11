"""Advanced analytics: performance vs live prices, benchmarks, recurring-spend
projections, and volatility-informed recommendations. Price lookups degrade
gracefully — with the market data source down, responses carry cost-basis
figures and price_available=false instead of failing."""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import RecurringPurchase, Transaction, User
from api.routes.user import get_current_user
import api.services.market_data as market_mod

router = APIRouter()

BUYS_PER_MONTH = {"daily": 30.44, "weekly": 4.35, "biweekly": 2.17, "monthly": 1.0, "one_time": 0.0}
ESTIMATES_NOTE = ("Estimates only, not financial advice; past performance doesn't guarantee "
                  "future results.")


class HoldingPerformance(BaseModel):
    crypto: str
    total_amount: float
    invested_usdt: float
    average_buy_price: float
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pnl_usdt: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None


class PerformanceResponse(BaseModel):
    price_available: bool
    holdings: List[HoldingPerformance]
    total_invested_usdt: float
    total_market_value: Optional[float] = None
    total_pnl_usdt: Optional[float] = None
    total_pnl_pct: Optional[float] = None
    best_performer: Optional[str] = None
    worst_performer: Optional[str] = None


async def _holdings(db, user_id):
    """[(crypto, amount, invested_usd)] from successful Auto-Buy buys (purchase_transactions)."""
    from api.services import history
    return await history.holdings(db, user_id)


@router.get("/performance", response_model=PerformanceResponse)
async def performance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unrealized P&L per holding against live prices, with best/worst performers."""
    holdings = []
    any_price = False
    for crypto, amount, invested in await _holdings(db, current_user.id):
        price = await market_mod.market_data.get_price(crypto)
        h = HoldingPerformance(
            crypto=crypto, total_amount=round(amount, 8),
            invested_usdt=round(invested, 2),
            average_buy_price=round(invested / amount, 8))
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
        by_pct = sorted(priced, key=lambda h: h.unrealized_pnl_pct)
        resp.best_performer = by_pct[-1].crypto
        resp.worst_performer = by_pct[0].crypto
    return resp


class ComparisonEntry(BaseModel):
    crypto: str
    average_buy_price: float
    current_price: Optional[float] = None
    buy_price_vs_market_pct: Optional[float] = None  # negative = bought below market


class ComparisonResponse(BaseModel):
    entries: List[ComparisonEntry]
    diversification_score: float  # 0 (one coin) .. 100 (evenly spread)


@router.get("/comparison", response_model=ComparisonResponse)
async def comparison(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Average buy price vs the market now, plus a diversification score
    (100 × (1 − Herfindahl index) over invested value)."""
    holdings = await _holdings(db, current_user.id)
    entries = []
    for crypto, amount, invested in holdings:
        avg = invested / amount
        price = await market_mod.market_data.get_price(crypto)
        e = ComparisonEntry(crypto=crypto, average_buy_price=round(avg, 8))
        if price is not None:
            e.current_price = price
            e.buy_price_vs_market_pct = round((avg - price) / price * 100, 2)
        entries.append(e)
    total = sum(inv for _, _, inv in holdings)
    hhi = sum((inv / total) ** 2 for _, _, inv in holdings) if total else 1.0
    return ComparisonResponse(entries=entries,
                              diversification_score=round((1 - hhi) * 100, 1))


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
    recommendation: str


class VolatilityResponse(BaseModel):
    entries: List[VolatilityEntry]


@router.get("/volatility", response_model=VolatilityResponse)
async def volatility(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """30-day daily-return volatility per held/scheduled coin, with a DCA-cadence
    suggestion: higher volatility rewards smaller, more frequent buys."""
    from api.models import Purchase
    cryptos = {c for c, _, _ in await _holdings(db, current_user.id)}
    # scheduled coins: active Auto-Buy purchases (both types) plus any legacy schedules still enabled
    cryptos |= {p.crypto_symbol for p in (await db.execute(
        select(Purchase).where(
            Purchase.user_id == current_user.id,
            Purchase.status == "active"))).scalars().all()}
    cryptos |= {p.crypto for p in (await db.execute(
        select(RecurringPurchase).where(
            RecurringPurchase.user_id == current_user.id,
            RecurringPurchase.enabled.is_(True)))).scalars().all()}
    entries = []
    for crypto in sorted(cryptos):
        vol = await market_mod.market_data.get_volatility_30d(crypto)
        if vol is None:
            entries.append(VolatilityEntry(
                crypto=crypto, level="unknown",
                recommendation="Volatility data unavailable right now."))
        elif vol >= 5:
            entries.append(VolatilityEntry(
                crypto=crypto, volatility_30d_pct=vol, level="high",
                recommendation="High volatility: smaller, more frequent buys "
                               "(e.g. weekly → daily) smooth your cost basis."))
        elif vol >= 2:
            entries.append(VolatilityEntry(
                crypto=crypto, volatility_30d_pct=vol, level="moderate",
                recommendation="Moderate volatility: your current cadence is fine."))
        else:
            entries.append(VolatilityEntry(
                crypto=crypto, volatility_30d_pct=vol, level="low",
                recommendation="Low volatility: less frequent, larger buys "
                               "would reduce fee overhead."))
    return VolatilityResponse(entries=entries)
