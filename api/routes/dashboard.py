"""User dashboard and analytics."""
from datetime import date, datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import RecurringPurchase, Transaction, User
from api.routes.user import UTCModel, get_current_user, to_db_datetime, utc_now

router = APIRouter()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class CryptoHolding(BaseModel):
    crypto: str
    total_amount: float
    total_cost_usdt: float


class ActivePurchase(UTCModel):
    id: UUID
    crypto: str
    amount_usdt: float
    frequency: str
    next_purchase_date: datetime


class UpcomingPurchase(UTCModel):
    id: UUID
    crypto: str
    amount_usdt: float
    frequency: str
    next_purchase_date: datetime


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
    """Sum completed transactions per crypto."""
    rows = (
        await db.execute(
            select(
                Transaction.crypto,
                func.coalesce(func.sum(Transaction.amount_crypto), 0.0),
                func.coalesce(func.sum(Transaction.cost_usdt), 0.0),
            )
            .where(Transaction.user_id == user_id, Transaction.status == "completed")
            .group_by(Transaction.crypto)
            .order_by(func.sum(Transaction.cost_usdt).desc())
        )
    ).all()
    return [
        CryptoHolding(
            crypto=row[0],
            total_amount=round(float(row[1]), 8),
            total_cost_usdt=round(float(row[2]), 8),
        )
        for row in rows
    ]


async def _monthly_average(db: AsyncSession, user_id) -> float:
    """Average USDT invested per calendar month (over months with activity)."""
    month = func.date_trunc("month", Transaction.created_at)
    rows = (
        await db.execute(
            select(month, func.sum(Transaction.cost_usdt))
            .where(Transaction.user_id == user_id, Transaction.status == "completed")
            .group_by(month)
        )
    ).all()
    if not rows:
        return 0.0
    return round(sum(float(r[1]) for r in rows) / len(rows), 8)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/", response_model=DashboardResponse)
async def dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Main dashboard: holdings, active schedules, upcoming buys, monthly average."""
    holdings = await _holdings(db, current_user.id)

    active = (
        await db.execute(
            select(RecurringPurchase)
            .where(
                RecurringPurchase.user_id == current_user.id,
                RecurringPurchase.enabled.is_(True),
            )
            .order_by(RecurringPurchase.next_purchase_date.asc())
        )
    ).scalars().all()

    horizon = to_db_datetime(utc_now() + timedelta(days=30))
    upcoming = [p for p in active if p.next_purchase_date <= horizon]

    return DashboardResponse(
        holdings=holdings,
        active_purchases=[ActivePurchase.model_validate(p) for p in active],
        upcoming_purchases=[UpcomingPurchase.model_validate(p) for p in upcoming],
        monthly_average_usdt=await _monthly_average(db, current_user.id),
    )


@router.get("/stats", response_model=QuickStats)
async def quick_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Quick overview numbers for the header/summary widgets."""
    total_invested = (
        await db.execute(
            select(func.coalesce(func.sum(Transaction.cost_usdt), 0.0)).where(
                Transaction.user_id == current_user.id,
                Transaction.status == "completed",
            )
        )
    ).scalar_one()
    tx_count = (
        await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == current_user.id
            )
        )
    ).scalar_one()
    active_count = (
        await db.execute(
            select(func.count(RecurringPurchase.id)).where(
                RecurringPurchase.user_id == current_user.id,
                RecurringPurchase.enabled.is_(True),
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
    """Daily transaction totals for the last `days` days (zero-filled)."""
    now = utc_now()
    start = to_db_datetime(now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day = func.date_trunc("day", Transaction.created_at)
    rows = (
        await db.execute(
            select(
                day,
                func.count(Transaction.id),
                func.coalesce(
                    func.sum(Transaction.cost_usdt).filter(
                        Transaction.status == "completed"
                    ),
                    0.0,
                ),
            )
            .where(
                Transaction.user_id == current_user.id,
                Transaction.created_at >= start,
            )
            .group_by(day)
        )
    ).all()

    by_day = {row[0].date(): (row[1], round(float(row[2]), 8)) for row in rows}
    timeline = []
    for offset in range(days):
        d = (start + timedelta(days=offset)).date()
        count, total = by_day.get(d, (0, 0.0))
        timeline.append(TimelinePoint(date=d, transaction_count=count, total_usdt=total))
    return TimelineResponse(days=days, timeline=timeline)
