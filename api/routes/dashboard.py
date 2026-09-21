"""User dashboard and analytics."""
from datetime import date, datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import Purchase, User
from api.services import history
from api.routes.user import UTCModel, get_current_user, to_db_datetime, utc_now
from fastapi import Request
from api.services.rate_limit import limiter, user_or_ip_key

router = APIRouter()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class CryptoHolding(BaseModel):
    """What the customer owns of a coin and where it is. total_amount is the
    quantity ACQUIRED (net of the trading fee charged in the coin); on_exchange
    is what is still on Bitget after real withdrawals (matches Bitget's balance);
    sent_to_wallet is what those withdrawals delivered."""
    crypto: str
    total_amount: float          # acquired, net of the coin-denominated trading fee
    on_exchange: float = 0.0
    sent_to_wallet: float = 0.0
    total_cost_usdt: float
    price_precision: int = 8   # decimals Bitget quotes the pair in (for any price derived from these figures)


class ActivePurchase(UTCModel):
    """An active Auto-Buy purchase (either type). Field names are kept from the
    original schedule shape so existing clients keep working: ``crypto`` is
    the purchase's crypto_symbol, ``amount_usdt`` its amount_usd and
    ``next_purchase_date`` its next_execution."""
    id: UUID
    crypto: str
    amount_usdt: float
    frequency: str
    next_purchase_date: Optional[datetime] = None
    purchase_type: str = "autobuy"   # autobuy (withdraw to wallet) | dca (accumulate on exchange)


class UpcomingPurchase(ActivePurchase):
    pass


def _schedule_row(cls, p: Purchase):
    return cls(id=p.id, crypto=p.crypto_symbol, amount_usdt=p.amount_usd, frequency=p.frequency,
               next_purchase_date=p.next_execution, purchase_type=p.purchase_type)


class DashboardResponse(BaseModel):
    holdings: List[CryptoHolding]
    active_purchases: List[ActivePurchase]
    upcoming_purchases: List[UpcomingPurchase]
    monthly_average_usdt: float


class QuickStats(BaseModel):
    total_invested_usdt: float
    transaction_count: int
    active_purchases: int
    kyc_status: str


class TimelinePoint(BaseModel):
    date: date
    transaction_count: int
    total_usdt: float


class TimelineResponse(BaseModel):
    days: int
    timeline: List[TimelinePoint]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _holdings(db: AsyncSession, user_id) -> List[CryptoHolding]:
    """Successful Auto-Buy buys summed per coin (purchase_transactions)."""
    from api.services import bitget_market
    return [CryptoHolding(crypto=pos.crypto, total_amount=pos.acquired, on_exchange=pos.on_exchange,
                          sent_to_wallet=pos.sent_to_wallet, total_cost_usdt=round(pos.cost_usd, 8),
                          price_precision=await bitget_market.price_precision_async(pos.crypto))
            for pos in await history.positions(db, user_id)]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/", response_model=DashboardResponse)
@limiter.limit("10/minute", key_func=user_or_ip_key)
async def dashboard(
    request: Request, response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Main dashboard: holdings, active Auto-Buy purchases (both ``autobuy``
    and ``dca`` types), the ones due within 30 days, and the monthly average."""
    holdings = await _holdings(db, current_user.id)

    active = (
        await db.execute(
            select(Purchase)
            .where(Purchase.user_id == current_user.id, Purchase.status == "active")
            .order_by(Purchase.next_execution.asc().nulls_last())
        )
    ).scalars().all()

    horizon = to_db_datetime(utc_now() + timedelta(days=30))
    upcoming = [p for p in active if p.next_execution is not None and p.next_execution <= horizon]

    return DashboardResponse(
        holdings=holdings,
        active_purchases=[_schedule_row(ActivePurchase, p) for p in active],
        upcoming_purchases=[_schedule_row(UpcomingPurchase, p) for p in upcoming],
        monthly_average_usdt=await history.monthly_average(db, current_user.id),
    )


@router.get("/stats", response_model=QuickStats)
async def quick_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Quick overview numbers for the header/summary widgets."""
    st = await history.stats(db, current_user.id)
    total_invested = st["total_invested_usdt"]
    tx_count = st["total_transactions"]
    active_count = (
        await db.execute(
            select(func.count(Purchase.id)).where(
                Purchase.user_id == current_user.id,
                Purchase.status == "active",
            )
        )
    ).scalar_one()
    return QuickStats(
        total_invested_usdt=round(float(total_invested), 8),
        transaction_count=tx_count,
        active_purchases=active_count,
        kyc_status=current_user.kyc_status,
    )


@router.get("/analytics/timeline", response_model=TimelineResponse)
async def analytics_timeline(
    days: int = Query(30, ge=1, le=365),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daily successful-buy totals for the last `days` days (zero-filled)."""
    points = await history.timeline(db, current_user.id, days, now=utc_now())
    return TimelineResponse(days=days, timeline=[TimelinePoint(**pt) for pt in points])
