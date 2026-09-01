"""Recurring purchase schedule CRUD."""
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import RecurringPurchase, User
from api.routes.user import UTCModel, get_current_user, to_db_datetime, utc_now

router = APIRouter()

VALID_FREQUENCIES = {"daily", "weekly", "biweekly", "monthly"}


def _validate_frequency(value: str) -> str:
    value = value.lower()
    if value not in VALID_FREQUENCIES:
        raise ValueError(
            f"frequency must be one of: {', '.join(sorted(VALID_FREQUENCIES))}"
        )
    return value


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class PurchaseCreate(BaseModel):
    crypto: str = Field(min_length=2, max_length=10)
    amount_usdt: float = Field(gt=0)
    frequency: str
    withdrawal_address: Optional[str] = Field(default=None, max_length=255)

    @field_validator("crypto")
    @classmethod
    def _upper(cls, value):
        return value.strip().upper()

    @field_validator("frequency")
    @classmethod
    def _freq(cls, value):
        return _validate_frequency(value)


class PurchaseUpdate(BaseModel):
    crypto: Optional[str] = Field(default=None, min_length=2, max_length=10)
    amount_usdt: Optional[float] = Field(default=None, gt=0)
    frequency: Optional[str] = None
    withdrawal_address: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None

    @field_validator("crypto")
    @classmethod
    def _upper(cls, value):
        return value.strip().upper() if value is not None else value

    @field_validator("frequency")
    @classmethod
    def _freq(cls, value):
        return _validate_frequency(value) if value is not None else value


class PurchaseResponse(UTCModel):
    id: UUID
    crypto: str
    amount_usdt: float
    frequency: str
    next_purchase_date: datetime
    withdrawal_address: Optional[str] = None
    enabled: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _get_owned_purchase(
    purchase_id: UUID, user: User, db: AsyncSession
) -> RecurringPurchase:
    result = await db.execute(
        select(RecurringPurchase).where(
            RecurringPurchase.id == purchase_id,
            RecurringPurchase.user_id == user.id,
        )
    )
    purchase = result.scalar_one_or_none()
    if purchase is None:
        raise HTTPException(status_code=404, detail="Recurring purchase not found")
    return purchase


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
async def create_purchase(
    payload: PurchaseCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a recurring purchase; the first buy is scheduled for tomorrow."""
    purchase = RecurringPurchase(
        user_id=current_user.id,
        crypto=payload.crypto,
        amount_usdt=payload.amount_usdt,
        frequency=payload.frequency,
        withdrawal_address=payload.withdrawal_address,
        next_purchase_date=to_db_datetime(utc_now() + timedelta(days=1)),
        enabled=True,
    )
    db.add(purchase)
    await db.commit()
    await db.refresh(purchase)
    return purchase


@router.get("/", response_model=List[PurchaseResponse])
async def list_purchases(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all of the authenticated user's recurring purchases."""
    result = await db.execute(
        select(RecurringPurchase)
        .where(RecurringPurchase.user_id == current_user.id)
        .order_by(RecurringPurchase.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase(
    purchase_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get one recurring purchase by id."""
    return await _get_owned_purchase(purchase_id, current_user, db)


@router.put("/{purchase_id}", response_model=PurchaseResponse)
async def update_purchase(
    purchase_id: UUID,
    payload: PurchaseUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update fields of a recurring purchase."""
    purchase = await _get_owned_purchase(purchase_id, current_user, db)

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    for field, value in updates.items():
        setattr(purchase, field, value)
    purchase.updated_at = to_db_datetime(utc_now())

    await db.commit()
    await db.refresh(purchase)
    return purchase


@router.delete("/{purchase_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_purchase(
    purchase_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a recurring purchase."""
    purchase = await _get_owned_purchase(purchase_id, current_user, db)
    await db.delete(purchase)
    await db.commit()


@router.patch("/{purchase_id}/toggle", response_model=PurchaseResponse)
async def toggle_purchase(
    purchase_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Flip a recurring purchase between enabled and disabled."""
    purchase = await _get_owned_purchase(purchase_id, current_user, db)
    purchase.enabled = not purchase.enabled
    purchase.updated_at = to_db_datetime(utc_now())
    await db.commit()
    await db.refresh(purchase)
    return purchase
