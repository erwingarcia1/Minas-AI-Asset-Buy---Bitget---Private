"""Transaction history and analytics."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import Transaction, User
from api.routes.user import UTCModel, get_current_user
from fastapi import Request
from api.services.rate_limit import limiter, user_or_ip_key

router = APIRouter()

VALID_STATUSES = {"pending", "completed", "failed"}


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

class TransactionResponse(UTCModel):
    id: UUID
    recurring_purchase_id: Optional[UUID] = None
    crypto: str
    amount_crypto: float
    cost_usdt: float
    binance_order_id: Optional[str] = None
    exchange_rate: Optional[float] = None
    executed_at: Optional[datetime] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


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


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/", response_model=TransactionListResponse)
@limiter.limit("30/minute", key_func=user_or_ip_key)
async def list_transactions(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the authenticated user's transactions, newest first, paginated."""
    filters = [Transaction.user_id == current_user.id]
    if status_filter is not None:
        filters.append(Transaction.status == _validate_status(status_filter))

    total = (
        await db.execute(select(func.count(Transaction.id)).where(*filters))
    ).scalar_one()
    result = await db.execute(
        select(Transaction)
        .where(*filters)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return TransactionListResponse(
        items=[TransactionResponse.model_validate(t) for t in result.scalars().all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/stats/summary", response_model=TransactionStats)
async def transaction_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate statistics over the user's transactions."""
    completed = Transaction.status == "completed"
    row = (
        await db.execute(
            select(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.cost_usdt).filter(completed), 0.0),
                func.coalesce(func.avg(Transaction.cost_usdt).filter(completed), 0.0),
                func.count(Transaction.id).filter(completed),
                func.count(Transaction.id).filter(Transaction.status == "failed"),
                func.count(Transaction.id).filter(Transaction.status == "pending"),
            ).where(Transaction.user_id == current_user.id)
        )
    ).one()
    return TransactionStats(
        total_transactions=row[0],
        total_invested_usdt=round(float(row[1]), 8),
        average_purchase_usdt=round(float(row[2]), 8),
        completed_count=row[3],
        failed_count=row[4],
        pending_count=row[5],
    )


@router.get("/crypto/{crypto}", response_model=List[TransactionResponse])
async def transactions_by_crypto(
    crypto: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's transactions for one cryptocurrency."""
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.user_id == current_user.id,
            Transaction.crypto == crypto.strip().upper(),
        )
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


@router.get("/status/{tx_status}", response_model=List[TransactionResponse])
async def transactions_by_status(
    tx_status: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's transactions with a given status."""
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.user_id == current_user.id,
            Transaction.status == _validate_status(tx_status),
        )
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get one transaction by id."""
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == current_user.id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return transaction
