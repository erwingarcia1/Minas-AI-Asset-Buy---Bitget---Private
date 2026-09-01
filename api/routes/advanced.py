"""Advanced purchase features: limit orders, rebalancing plans, and
opportunity detection (buy-only rebalancing suggestions plus volatility
guidance). Plain schedules stay in /api/purchases."""
from datetime import timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import RecurringPurchase, Transaction, User
from api.routes.purchases import (
    MAX_AMOUNT_USDT, MIN_AMOUNT_USDT, PurchaseResponse, _validate_frequency,
    validate_crypto)
from api.routes.user import get_current_user, to_db_datetime, utc_now
import api.services.market_data as market_mod

router = APIRouter()


class LimitOrderCreate(BaseModel):
    crypto: str
    amount_usdt: float = Field(ge=MIN_AMOUNT_USDT, le=MAX_AMOUNT_USDT)
    limit_price: float = Field(gt=0)
    expiry_days: int = Field(default=30, ge=1, le=365)

    _crypto = field_validator("crypto")(classmethod(lambda cls, v: validate_crypto(v)))


class LimitOrderResponse(PurchaseResponse):
    limit_price: Optional[float] = None
    purchase_type: str
    expires_at: Optional[str] = None

    class Config:
        from_attributes = True


@router.post("/limit-order", response_model=PurchaseResponse, status_code=201)
async def create_limit_order(
    payload: LimitOrderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One-shot buy that executes only once the price is at or below
    `limit_price`; checked hourly, expires unexecuted after `expiry_days`."""
    order = RecurringPurchase(
        user_id=current_user.id,
        crypto=payload.crypto,
        amount_usdt=payload.amount_usdt,
        frequency="daily",  # cadence of the price re-check only
        purchase_type="limit_order",
        limit_price=payload.limit_price,
        next_purchase_date=to_db_datetime(utc_now()),
        expires_at=to_db_datetime(utc_now() + timedelta(days=payload.expiry_days)),
        enabled=True,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


class RebalanceCreate(BaseModel):
    target_allocations: Dict[str, float]  # {"BTC": 60, "ETH": 40}
    amount_usdt: float = Field(ge=MIN_AMOUNT_USDT, le=MAX_AMOUNT_USDT)
    frequency: str

    @field_validator("frequency")
    @classmethod
    def _freq(cls, v):
        return _validate_frequency(v)

    @field_validator("target_allocations")
    @classmethod
    def _alloc(cls, value):
        if not value:
            raise ValueError("target_allocations must not be empty")
        cleaned = {validate_crypto(c): float(p) for c, p in value.items()}
        total = sum(cleaned.values())
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"allocations must sum to 100 (got {total})")
        if any(p <= 0 for p in cleaned.values()):
            raise ValueError("each allocation must be positive")
        return cleaned


class RebalancePlanEntry(BaseModel):
    crypto: str
    target_pct: float
    amount_usdt: float
    purchase_id: str


class RebalancePlan(BaseModel):
    frequency: str
    total_per_period_usdt: float
    entries: List[RebalancePlanEntry]


@router.post("/rebalance", response_model=RebalancePlan, status_code=201)
async def create_rebalance_plan(
    payload: RebalanceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Split a per-period budget across target allocations as a set of
    rebalancing schedules. Per-coin slices below the 10 USDT venue minimum
    are rejected outright rather than silently dropped."""
    slices = {c: round(payload.amount_usdt * p / 100, 2)
              for c, p in payload.target_allocations.items()}
    too_small = [c for c, amt in slices.items() if amt < MIN_AMOUNT_USDT]
    if too_small:
        raise HTTPException(
            status_code=422,
            detail=f"Allocation slice(s) below the {MIN_AMOUNT_USDT} USDT minimum: "
                   f"{', '.join(sorted(too_small))} — raise amount_usdt or the percentages")
    existing = (await db.execute(
        select(RecurringPurchase).where(
            RecurringPurchase.user_id == current_user.id,
            RecurringPurchase.purchase_type == "rebalancing",
            RecurringPurchase.enabled.is_(True)))).scalars().all()
    if existing:
        raise HTTPException(
            status_code=422,
            detail="An active rebalancing plan already exists; disable it first")

    entries = []
    first_due = to_db_datetime(utc_now() + timedelta(days=1))
    for crypto, pct in payload.target_allocations.items():
        p = RecurringPurchase(
            user_id=current_user.id, crypto=crypto, amount_usdt=slices[crypto],
            frequency=payload.frequency, purchase_type="rebalancing",
            target_allocation=pct, next_purchase_date=first_due, enabled=True)
        db.add(p)
        await db.flush()
        entries.append(RebalancePlanEntry(
            crypto=crypto, target_pct=pct, amount_usdt=slices[crypto],
            purchase_id=str(p.id)))
    await db.commit()
    return RebalancePlan(frequency=payload.frequency,
                         total_per_period_usdt=payload.amount_usdt,
                         entries=entries)


class Opportunity(BaseModel):
    crypto: str
    kind: str        # underweight | overweight | volatility
    detail: str
    suggested_next_period_usdt: Optional[float] = None


class OpportunitiesResponse(BaseModel):
    price_available: bool
    opportunities: List[Opportunity]


@router.get("/opportunities", response_model=OpportunitiesResponse)
async def opportunities(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Buy-only rebalancing drift analysis: compares current market-value
    weights to the active plan's targets and suggests skewing the next
    period's budget toward underweights. Never suggests selling."""
    plan = (await db.execute(
        select(RecurringPurchase).where(
            RecurringPurchase.user_id == current_user.id,
            RecurringPurchase.purchase_type == "rebalancing",
            RecurringPurchase.enabled.is_(True)))).scalars().all()
    if not plan:
        return OpportunitiesResponse(price_available=False, opportunities=[])

    from sqlalchemy import func
    rows = (await db.execute(
        select(Transaction.crypto, func.sum(Transaction.amount_crypto))
        .where(Transaction.user_id == current_user.id,
               Transaction.status == "completed")
        .group_by(Transaction.crypto))).all()
    amounts = {r[0]: float(r[1]) for r in rows}

    values = {}
    for p in plan:
        price = await market_mod.market_data.get_price(p.crypto)
        if price is None:
            return OpportunitiesResponse(price_available=False, opportunities=[])
        values[p.crypto] = amounts.get(p.crypto, 0.0) * price
    total_value = sum(values.values())
    budget = sum(p.amount_usdt for p in plan)

    opps = []
    if total_value > 0:
        gaps = {}
        for p in plan:
            current_pct = values[p.crypto] / total_value * 100
            gap = p.target_allocation - current_pct
            gaps[p.crypto] = max(gap, 0.0)
            kind = "underweight" if gap > 1 else "overweight" if gap < -1 else "on-target"
            if kind != "on-target":
                opps.append(Opportunity(
                    crypto=p.crypto, kind=kind,
                    detail=f"currently {current_pct:.1f}% vs target "
                           f"{p.target_allocation:.0f}%"))
        gap_total = sum(gaps.values())
        if gap_total > 0:
            for o in opps:
                if o.kind == "underweight":
                    o.suggested_next_period_usdt = round(
                        budget * gaps[o.crypto] / gap_total, 2)
    for p in plan:
        vol = await market_mod.market_data.get_volatility_30d(p.crypto)
        if vol is not None and vol >= 5:
            opps.append(Opportunity(
                crypto=p.crypto, kind="volatility",
                detail=f"30d volatility {vol}% — smaller, more frequent buys "
                       "smooth your cost basis (enable volatility_enabled)"))
    return OpportunitiesResponse(price_available=True, opportunities=opps)
