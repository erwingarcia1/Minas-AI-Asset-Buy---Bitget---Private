"""Transaction history: every Auto-Buy execution attempt, read through the
history service (purchase_transactions is the single source of truth)."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import User
from api.routes.user import UTCModel, get_current_user
from api.services import history
from api.services.rate_limit import limiter, user_or_ip_key

router = APIRouter()

VALID_STATUSES = set(history.API_STATUSES)


def _validate_status(value: str) -> str:
    value = value.lower()
    if value not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {', '.join(sorted(VALID_STATUSES))}",
        )
    return value


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

async def _precision(coin: str, memo: dict) -> int:
    """Pair price precision, one Bitget cache read per coin per request."""
    if coin not in memo:
        from api.services import bitget_market
        memo[coin] = await bitget_market.price_precision_async(coin)
    return memo[coin]


class TransactionResponse(UTCModel):
    """One execution attempt. ``cost_usdt``/``exchange_rate``/``binance_order_id``
    keep their historical names for existing clients; ``order_id`` and the
    withdrawal fields are the new, explicit ones."""
    id: UUID
    purchase_id: UUID
    crypto: str
    purchase_type: str                 # autobuy | dca
    tx_type: str                       # withdrawal | accumulation
    amount_crypto: float
    cost_usdt: float
    exchange_rate: Optional[float] = None
    price_precision: int = 8           # decimals Bitget quotes the pair in; show exchange_rate with exactly these
    exchange: Optional[str] = None
    order_id: Optional[str] = None
    binance_order_id: Optional[str] = None   # = order_id (compatibility)
    fee: Optional[float] = None
    fee_currency: Optional[str] = None
    withdrawal_id: Optional[str] = None
    wallet_address: Optional[str] = None
    blockchain_network: Optional[str] = None
    withdrawal_fee: Optional[float] = None
    withdrawal_fee_currency: Optional[str] = None
    tx_hash: Optional[str] = None
    executed_at: Optional[datetime] = None
    status: str                        # pending | completed | failed
    error_message: Optional[str] = None
    created_at: datetime

    @classmethod
    def from_row(cls, r: history.HistoryRow, price_precision: int = 8) -> "TransactionResponse":
        return cls(id=r.id, purchase_id=r.purchase_id, crypto=r.crypto, purchase_type=r.purchase_type,
                   tx_type=r.tx_type, amount_crypto=r.amount_crypto, cost_usdt=r.cost_usd,
                   exchange_rate=r.exchange_rate, price_precision=price_precision, exchange=r.exchange, order_id=r.order_id,
                   binance_order_id=r.order_id, fee=r.fee, fee_currency=r.fee_currency,
                   withdrawal_id=r.withdrawal_id, wallet_address=r.wallet_address,
                   blockchain_network=r.blockchain_network, withdrawal_fee=r.withdrawal_fee,
                   withdrawal_fee_currency=r.withdrawal_fee_currency, tx_hash=r.tx_hash,
                   executed_at=r.executed_at, status=r.status, error_message=r.error_message,
                   created_at=r.created_at)


class TransactionListResponse(BaseModel):
    items: List[TransactionResponse]
    total: int
    limit: int
    offset: int


class TransactionStats(BaseModel):
    total_transactions: int
    total_invested_usdt: float
    average_purchase_usdt: float
    completed_count: int
    failed_count: int
    pending_count: int
    needs_review_count: int = 0   # attempts parked for a human (implausible or unreadable fill)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/", response_model=TransactionListResponse)
@limiter.limit("30/minute", key_func=user_or_ip_key)
async def list_transactions(
    request: Request, response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the authenticated user's execution attempts, newest first, paginated."""
    _prec: dict = {}   # pair precision memo, one Bitget cache read per coin per request
    status = _validate_status(status_filter) if status_filter is not None else None
    rows, total = await history.list_rows(db, current_user.id, status=status, limit=limit, offset=offset)
    return TransactionListResponse(items=[TransactionResponse.from_row(r, await _precision(r.crypto, _prec)) for r in rows],
                                   total=total, limit=limit, offset=offset)


@router.get("/stats/summary", response_model=TransactionStats)
async def transaction_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate statistics over the user's attempts (invested = successful buys)."""
    return TransactionStats(**await history.stats(db, current_user.id))


@router.get("/crypto/{crypto}", response_model=List[TransactionResponse])
async def transactions_by_crypto(
    crypto: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's attempts for one cryptocurrency."""
    _prec: dict = {}   # pair precision memo, one Bitget cache read per coin per request
    rows, _ = await history.list_rows(db, current_user.id, crypto=crypto, limit=limit, offset=offset)
    return [TransactionResponse.from_row(r, await _precision(r.crypto, _prec)) for r in rows]


@router.get("/status/{tx_status}", response_model=List[TransactionResponse])
async def transactions_by_status(
    tx_status: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's attempts with a given status."""
    _prec: dict = {}   # pair precision memo, one Bitget cache read per coin per request
    rows, _ = await history.list_rows(db, current_user.id, status=_validate_status(tx_status),
                                      limit=limit, offset=offset)
    return [TransactionResponse.from_row(r, await _precision(r.crypto, _prec)) for r in rows]


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get one attempt by id."""
    _prec: dict = {}   # pair precision memo, one Bitget cache read per coin per request
    row = await history.get_row(db, current_user.id, transaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return TransactionResponse.from_row(row, await _precision(row.crypto, _prec))
