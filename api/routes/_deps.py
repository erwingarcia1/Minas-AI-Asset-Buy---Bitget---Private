"""Helpers shared by route modules (extracted 2026-09-22, audit Batch B2).

Each helper keeps the exact status code and message its former copies used:
"User not found" / "Purchase not found" as plain-string 404s.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.models import Purchase, User


async def user_or_404(db: AsyncSession, user_id: UUID) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def purchase_or_404(db: AsyncSession, purchase_id: UUID, *, owner: Optional[User] = None,
                          eager: bool = True, populate_existing: bool = False) -> Purchase:
    """Load a purchase or answer 404.

    owner: scope to that user's purchases (customer routes); None = any (admin).
    eager: load transactions, exchange_account and user in the same round trip.
    populate_existing: refresh an instance already in the session (admin review
    reads after the executor wrote in another session)."""
    stmt = select(Purchase)
    if eager:
        stmt = stmt.options(selectinload(Purchase.transactions), selectinload(Purchase.exchange_account),
                            selectinload(Purchase.user))
    if populate_existing:
        stmt = stmt.execution_options(populate_existing=True)
    stmt = stmt.where(Purchase.id == purchase_id)
    if owner is not None:
        stmt = stmt.where(Purchase.user_id == owner.id)
    p = (await db.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
    return p


def gateway():
    """The live exchange gateway (imported lazily: the executor module pulls in the scheduler)."""
    from api.tasks.purchase_scheduler import ExchangeClientGateway
    return ExchangeClientGateway()


class PrecisionMemo:
    """Pair price precision, one Bitget cache read per coin per request."""

    def __init__(self) -> None:
        self._memo: dict = {}

    async def get(self, coin: str) -> int:
        if coin not in self._memo:
            from api.services import bitget_market
            self._memo[coin] = await bitget_market.price_precision_async(coin)
        return self._memo[coin]


def precision_memo() -> PrecisionMemo:
    """FastAPI dependency: a fresh memo per request (adds nothing to the OpenAPI schema)."""
    return PrecisionMemo()
