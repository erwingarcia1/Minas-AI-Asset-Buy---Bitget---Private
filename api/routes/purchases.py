"""Recurring purchase schedule CRUD."""
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.models import RecurringPurchase, User
from api.routes.user import UTCModel, get_current_user, to_db_datetime, utc_now

router = APIRouter()

VALID_FREQUENCIES = {"daily", "weekly", "biweekly", "monthly"}

# Binance SPOT effective minimum; sanity ceiling against fat-finger schedules.
MIN_AMOUNT_USDT = 10.0
MAX_AMOUNT_USDT = 1_000_000.0

# Address shape per chain. Deliberately permissive within each chain's format —
# this catches wrong-chain and mangled addresses, not typos within a valid shape.
ADDRESS_PATTERNS = {
    "BTC": r"^(1|3)[a-km-zA-HJ-NP-Z1-9]{25,34}$|^bc1[a-z0-9]{25,90}$",
    "ETH": r"^0x[a-fA-F0-9]{40}$",
    "BNB": r"^0x[a-fA-F0-9]{40}$|^bnb1[a-z0-9]{38}$",
    "SOL": r"^[1-9A-HJ-NP-Za-km-z]{32,44}$",
    "XRP": r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    "ADA": r"^addr1[a-z0-9]{50,120}$",
    "DOGE": r"^D[a-km-zA-HJ-NP-Z1-9]{25,34}$",
    "LTC": r"^(L|M)[a-km-zA-HJ-NP-Z1-9]{26,33}$|^ltc1[a-z0-9]{25,90}$",
}


def supported_cryptos() -> set:
    return {c.strip().upper() for c in settings.supported_cryptos.split(",") if c.strip()}


def validate_crypto(value: str) -> str:
    value = value.strip().upper()
    allowed = supported_cryptos()
    if value not in allowed:
        raise ValueError(f"crypto must be one of: {', '.join(sorted(allowed))}")
    return value


def validate_withdrawal_address(crypto: str, address: str) -> None:
    """Raises ValueError when `address` does not fit `crypto`'s address format."""
    import re
    pattern = ADDRESS_PATTERNS.get(crypto.upper())
    if pattern and not re.match(pattern, address):
        raise ValueError(f"withdrawal_address is not a valid {crypto.upper()} address")


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
    amount_usdt: float = Field(ge=MIN_AMOUNT_USDT, le=MAX_AMOUNT_USDT)
    frequency: str
    withdrawal_address: Optional[str] = Field(default=None, max_length=255)

    @field_validator("crypto")
    @classmethod
    def _crypto(cls, value):
        return validate_crypto(value)

    @field_validator("frequency")
    @classmethod
    def _freq(cls, value):
        return _validate_frequency(value)

    @model_validator(mode="after")
    def _address_matches_crypto(self):
        if self.withdrawal_address:
            validate_withdrawal_address(self.crypto, self.withdrawal_address)
        return self


class PurchaseUpdate(BaseModel):
    crypto: Optional[str] = Field(default=None, min_length=2, max_length=10)
    amount_usdt: Optional[float] = Field(default=None, ge=MIN_AMOUNT_USDT, le=MAX_AMOUNT_USDT)
    frequency: Optional[str] = None
    withdrawal_address: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None

    @field_validator("crypto")
    @classmethod
    def _crypto(cls, value):
        return validate_crypto(value) if value is not None else value

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
    dup = (await db.execute(
        select(RecurringPurchase).where(
            RecurringPurchase.user_id == current_user.id,
            RecurringPurchase.crypto == payload.crypto,
            RecurringPurchase.enabled.is_(True),
        ))).scalars().first()
    if dup is not None:
        raise HTTPException(
            status_code=422,
            detail=f"An active {payload.crypto} schedule already exists (id {dup.id}); "
                   "edit or disable it instead of creating a duplicate")
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
    if purchase.withdrawal_address:
        try:
            validate_withdrawal_address(purchase.crypto, purchase.withdrawal_address)
        except ValueError as exc:
            await db.rollback()
            raise HTTPException(status_code=422, detail=str(exc))
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
