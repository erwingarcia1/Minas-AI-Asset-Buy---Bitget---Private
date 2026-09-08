"""Auto-Buy purchases: buy on an exchange account and deliver to a validated
wallet on a declared blockchain network. Mounted at /api/v2/purchases.

Every request validates (1) the wallet address format against the coin's
chain and the declared network, (2) that the exchange account can withdraw
to that network, and (3) that the caller owns the exchange account.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.database import get_db
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow
from api.models.purchase import BLOCKCHAIN_NETWORKS, PURCHASE_FREQUENCIES
from api.routes.user import get_current_user
from api.utils.exchange_networks import (
    SUPPORTED_CRYPTOS, can_exchange_withdraw_to_network, chain_for_crypto, supported_networks)
from api.utils.network_validator import validate_address_for_network

logger = logging.getLogger("autobuy.purchases.v2")

router = APIRouter()

MIN_AMOUNT_USD, MAX_AMOUNT_USD = 10.0, 1_000_000.0


# --------------------------------------------------------------------------- schemas

class PurchaseCreateRequest(BaseModel):
    crypto_symbol: str = Field(min_length=2, max_length=10)
    amount_usd: float = Field(ge=MIN_AMOUNT_USD, le=MAX_AMOUNT_USD)
    wallet_address: Optional[str] = Field(default=None, max_length=128)
    blockchain_network: str
    frequency: str
    exchange_account_id: UUID
    # True  = Auto-Buy: buy, then withdraw to wallet_address right away (wallet required)
    # False = DCA accumulate: buy and hold on the exchange (wallet optional, kept for later withdrawals)
    withdraw_immediately: bool = True

    @field_validator("crypto_symbol")
    @classmethod
    def _crypto(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in SUPPORTED_CRYPTOS:
            raise ValueError(f"unsupported crypto; choose one of {', '.join(SUPPORTED_CRYPTOS)}")
        return v

    @field_validator("blockchain_network")
    @classmethod
    def _network(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in BLOCKCHAIN_NETWORKS:
            raise ValueError("blockchain_network must be 'mainnet' or 'testnet'")
        return v

    @field_validator("frequency")
    @classmethod
    def _frequency(cls, v: str) -> str:
        v = v.lower().strip().replace("-", "_").replace(" ", "_")
        if v == "once":
            v = "one_time"
        if v not in PURCHASE_FREQUENCIES:
            raise ValueError(f"frequency must be one of {', '.join(PURCHASE_FREQUENCIES)}")
        return v

    @field_validator("wallet_address")
    @classmethod
    def _address(cls, v):
        v = (v or "").strip()
        return v or None


class PurchaseUpdateRequest(BaseModel):
    status: Optional[str] = None            # active | paused
    amount_usd: Optional[float] = Field(default=None, ge=MIN_AMOUNT_USD, le=MAX_AMOUNT_USD)
    frequency: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is None:
            return v
        v = v.lower().strip()
        if v not in ("active", "paused"):
            raise ValueError("status must be 'active' (resume) or 'paused' (pause)")
        return v

    @field_validator("frequency")
    @classmethod
    def _frequency(cls, v):
        return None if v is None else PurchaseCreateRequest._frequency(v)


class TransactionOut(BaseModel):
    id: UUID
    purchase_id: UUID
    exchange_tx_id: Optional[str] = None
    status: str
    amount_executed: Optional[float] = None
    fee: Optional[float] = None
    error_message: Optional[str] = None
    executed_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PurchaseOut(BaseModel):
    id: UUID
    crypto_symbol: str
    amount_usd: float
    frequency: str
    wallet_address: Optional[str] = None
    blockchain_network: str
    withdraw_immediately: bool
    exchange_account_id: Optional[UUID] = None
    exchange: Optional[str] = None
    status: str
    failure_count: int
    last_error: Optional[str] = None
    next_execution: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    latest_transaction: Optional[TransactionOut] = None

    model_config = {"from_attributes": True}


class PurchaseDetailOut(PurchaseOut):
    transactions: List[TransactionOut] = []


class PurchaseCreatedOut(BaseModel):
    purchase_id: UUID
    status: str
    next_execution: Optional[datetime] = None


# --------------------------------------------------------------------------- helpers

def _to_out(p: Purchase, detail: bool = False) -> PurchaseOut:
    txs = list(p.transactions or [])
    txs.sort(key=lambda t: t.created_at, reverse=True)
    base = dict(
        id=p.id, crypto_symbol=p.crypto_symbol, amount_usd=p.amount_usd, frequency=p.frequency,
        wallet_address=p.wallet_address, blockchain_network=p.blockchain_network,
        withdraw_immediately=p.withdraw_immediately, exchange_account_id=p.exchange_account_id,
        exchange=p.exchange_account.exchange if p.exchange_account else None,
        status=p.status, failure_count=p.failure_count, last_error=p.last_error,
        next_execution=p.next_execution, created_at=p.created_at, updated_at=p.updated_at,
        latest_transaction=TransactionOut.model_validate(txs[0]) if txs else None,
    )
    if detail:
        return PurchaseDetailOut(**base, transactions=[TransactionOut.model_validate(t) for t in txs])
    return PurchaseOut(**base)


async def _owned_purchase(db: AsyncSession, purchase_id: UUID, user: User) -> Purchase:
    p = (await db.execute(
        select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.exchange_account))
        .where(Purchase.id == purchase_id, Purchase.user_id == user.id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
    return p


def _reject(error: str, details: dict) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": error, "details": details})


# --------------------------------------------------------------------------- routes

@router.post("", response_model=PurchaseCreatedOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=PurchaseCreatedOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_purchase(
    payload: PurchaseCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a purchase in one of two modes: `withdraw_immediately: true`
    (Auto-Buy: buy, then send the coins to `wallet_address`; wallet required)
    or `false` (DCA accumulate: buy and hold on the exchange; wallet optional).
    Rejects (422 with `{error, details}`) when a given wallet address does not
    match the coin's chain or the declared network, when the exchange account
    cannot withdraw to that network (immediate mode), or when the exchange
    account is not the caller's."""
    chain = chain_for_crypto(payload.crypto_symbol)
    if payload.withdraw_immediately and not payload.wallet_address:
        raise _reject("wallet_address_required", {
            "withdraw_immediately": True,
            "message": "A wallet address is required when withdrawing immediately; untick it to accumulate on the exchange",
        })
    if payload.wallet_address and not validate_address_for_network(payload.wallet_address, payload.blockchain_network, chain=chain):
        raise _reject("invalid_wallet_address", {
            "wallet_address": payload.wallet_address, "blockchain_network": payload.blockchain_network,
            "expected_chain": chain,
            "message": f"Address is not a valid {chain} {payload.blockchain_network} address for {payload.crypto_symbol}",
        })

    account = (await db.execute(select(APIKey).where(
        APIKey.id == payload.exchange_account_id, APIKey.user_id == current_user.id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail={"error": "exchange_account_not_found",
                                                    "details": {"exchange_account_id": str(payload.exchange_account_id)}})

    if payload.withdraw_immediately and not can_exchange_withdraw_to_network(account.exchange, payload.blockchain_network):
        raise _reject("network_not_supported_by_exchange", {
            "exchange": account.exchange, "blockchain_network": payload.blockchain_network,
            "supported_networks": supported_networks(account.exchange),
        })

    purchase = Purchase(
        user_id=current_user.id, exchange_account_id=account.id, crypto_symbol=payload.crypto_symbol,
        amount_usd=payload.amount_usd, frequency=payload.frequency, wallet_address=payload.wallet_address,
        blockchain_network=payload.blockchain_network, withdraw_immediately=payload.withdraw_immediately,
        status="active", failure_count=0,
        next_execution=utcnow(),  # due on the scheduler's next run
    )
    db.add(purchase)
    await db.commit()
    await db.refresh(purchase)
    logger.info("purchase created", extra={"purchase_id": str(purchase.id), "user_id": str(current_user.id),
                                           "crypto": purchase.crypto_symbol, "network": purchase.blockchain_network,
                                           "withdraw_immediately": purchase.withdraw_immediately})
    return PurchaseCreatedOut(purchase_id=purchase.id, status=purchase.status, next_execution=purchase.next_execution)


@router.get("", response_model=List[PurchaseOut])
@router.get("/", response_model=List[PurchaseOut], include_in_schema=False)
async def list_purchases(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """All of the caller's Auto-Buy purchases, each with its latest transaction."""
    rows = (await db.execute(
        select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.exchange_account))
        .where(Purchase.user_id == current_user.id).order_by(Purchase.created_at.desc()))).scalars().all()
    return [_to_out(p) for p in rows]


@router.get("/{purchase_id}", response_model=PurchaseDetailOut)
async def get_purchase(purchase_id: UUID, current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """One purchase with its full transaction history (newest first)."""
    return _to_out(await _owned_purchase(db, purchase_id, current_user), detail=True)


@router.put("/{purchase_id}", response_model=PurchaseOut)
async def update_purchase(purchase_id: UUID, payload: PurchaseUpdateRequest,
                          current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Pause (`status: paused`), resume (`status: active`) or change amount/frequency.
    Cancelled, completed and failed purchases cannot be changed."""
    p = await _owned_purchase(db, purchase_id, current_user)
    if p.status in ("cancelled", "completed", "failed"):
        raise HTTPException(status_code=409, detail=f"Purchase is {p.status} and cannot be changed")
    if payload.status is None and payload.amount_usd is None and payload.frequency is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if payload.status == "active" and p.status == "paused":
        p.failure_count = 0
        p.last_error = None
        if p.next_execution is None or p.next_execution < utcnow():
            p.next_execution = utcnow()
    if payload.status is not None:
        p.status = payload.status
    if payload.amount_usd is not None:
        p.amount_usd = payload.amount_usd
    if payload.frequency is not None:
        p.frequency = payload.frequency
    await db.commit()
    await db.refresh(p)
    return _to_out(p)


@router.delete("/{purchase_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def cancel_purchase(purchase_id: UUID, current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """Cancel a purchase. The record and its transaction history are kept;
    the scheduler will never execute it again."""
    p = await _owned_purchase(db, purchase_id, current_user)
    p.status = "cancelled"
    p.next_execution = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{purchase_id}/transactions", response_model=List[TransactionOut])
async def purchase_transactions(purchase_id: UUID, status_filter: Optional[str] = None,
                                current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Transaction history for a purchase, optionally filtered by status
    (pending / success / failed)."""
    p = await _owned_purchase(db, purchase_id, current_user)
    txs = sorted(p.transactions, key=lambda t: t.created_at, reverse=True)
    if status_filter:
        txs = [t for t in txs if t.status == status_filter.lower()]
    return [TransactionOut.model_validate(t) for t in txs]


# re-export for tests
__all__ = ["router", "PurchaseCreateRequest", "timedelta", "PurchaseTransaction"]
