"""Admin routes — Parked purchases: review queue, live exchange lookup, resolve, force-retry, fill deviations.

Split out of api/routes/admin.py on 2026-09-22 (audit Batch B4); every route
still requires role=admin and every state change writes an audit line."""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.config import settings
from api.services.admin_alerts import AlertItem, compute_alerts
from api.models import Purchase, PurchaseTransaction, User
from api.routes.user import UTCModel, get_current_user

from api.services import withdraw_gate
from api.routes.admin._deps import VALID_KYC, audit, logger, require_admin  # noqa: F401
from api.routes._deps import gateway as _gateway_impl, purchase_or_404

router = APIRouter()


def _gateway():
    """Module-level so tests can patch it; the implementation lives in api.routes._deps."""
    return _gateway_impl()


class ParkedAttempt(UTCModel):
    id: UUID
    client_oid: Optional[str] = None
    order_id: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    executed_at: Optional[datetime] = None


class AdminPurchaseRow(UTCModel):
    id: UUID
    user_id: UUID
    email: Optional[str] = None
    crypto_symbol: str
    amount_usd: float
    frequency: str
    purchase_type: str
    status: str
    failure_count: int
    last_error: Optional[str] = None
    next_execution: Optional[datetime] = None
    updated_at: datetime
    parked_attempts: List[ParkedAttempt] = []


class AdminPurchaseList(BaseModel):
    items: List[AdminPurchaseRow]
    total: int


def _purchase_row(p: Purchase) -> AdminPurchaseRow:
    parked = [t for t in (p.transactions or []) if t.status == "needs_review"]
    return AdminPurchaseRow(id=p.id, user_id=p.user_id, email=p.user.email if p.user else None, crypto_symbol=p.crypto_symbol,
                            amount_usd=p.amount_usd, frequency=p.frequency, purchase_type=p.purchase_type, status=p.status,
                            failure_count=p.failure_count or 0, last_error=p.last_error, next_execution=p.next_execution,
                            updated_at=p.updated_at,
                            parked_attempts=[ParkedAttempt.model_validate(t) for t in sorted(parked, key=lambda t: t.created_at)])


async def _load_purchase(db: AsyncSession, purchase_id: UUID) -> Purchase:
    return await purchase_or_404(db, purchase_id, populate_existing=True)


@router.get("/purchases", response_model=AdminPurchaseList)
async def list_purchases(status: Optional[str] = Query("needs_review"), limit: int = Query(50, ge=1, le=200),
                         offset: int = Query(0, ge=0), admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    """Purchases by status (default: the parked ones), with their parked attempts."""
    from sqlalchemy.orm import selectinload
    filters = [Purchase.status == status] if status else []
    total = (await db.execute(select(func.count(Purchase.id)).where(*filters))).scalar_one()
    rows = (await db.execute(select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.user))
                             .where(*filters).order_by(Purchase.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return AdminPurchaseList(items=[_purchase_row(p) for p in rows], total=total)


class ExchangeLookup(BaseModel):
    attempt_id: UUID
    client_oid: Optional[str] = None
    state: Optional[str] = None          # filled | open | cancelled | not_found | error
    fill: Optional[dict] = None
    verdict: Optional[str] = None        # "ok" when the fill could be recorded, else why not
    error: Optional[str] = None


class PurchaseReview(BaseModel):
    purchase: AdminPurchaseRow
    lookups: List[ExchangeLookup]
    actions: List[str]


async def _lookup_parked(p: Purchase) -> List[ExchangeLookup]:
    """Ask the exchange, by our client id, what became of each parked attempt."""
    import asyncio
    from api.tasks.purchase_scheduler import validate_fill
    out = []
    gw = _gateway()
    for t in sorted((t for t in (p.transactions or []) if t.status == "needs_review"), key=lambda t: t.created_at):
        row = ExchangeLookup(attempt_id=t.id, client_oid=t.client_oid)
        if p.exchange_account is None or not t.client_oid:
            row.state, row.error = "error", "no exchange key or client id to look up"
        else:
            try:
                found = await asyncio.to_thread(gw.lookup_order, p.exchange_account, p, t.client_oid)
                row.state, row.fill = found.get("state"), found.get("fill")
                if row.state == "filled":
                    problem = validate_fill(found["fill"], float(p.amount_usd))
                    row.verdict = "ok" if problem is None else problem
            except Exception as exc:
                row.state, row.error = "error", str(exc)[:300]
        out.append(row)
    return out


@router.get("/purchases/{purchase_id}/review", response_model=PurchaseReview)
async def review_purchase(purchase_id: UUID, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Inspect a parked purchase: its attempts and, live from the exchange, what
    became of each order (state, fill, and whether the fill would pass the
    guard). Read-only. Then POST /resolve."""
    p = await _load_purchase(db, purchase_id)
    return PurchaseReview(purchase=_purchase_row(p), lookups=await _lookup_parked(p),
                          actions=["record_fill", "mark_not_sent", "cancel"])


class ResolvePurchase(BaseModel):
    action: str = Field(pattern="^(record_fill|mark_not_sent|cancel)$")
    note: str = Field(min_length=5, max_length=500)    # what was checked on the exchange
    attempt_id: Optional[UUID] = None                   # which parked attempt (default: the latest)
    # record_fill on a withdraw-mode purchase: keep the coins on the exchange (no withdrawal) and
    # switch the purchase to accumulate. Required when the owner is not allowed withdrawals.
    keep_on_exchange: bool = False


@router.post("/purchases/{purchase_id}/resolve", response_model=AdminPurchaseRow)
async def resolve_purchase(purchase_id: UUID, payload: ResolvePurchase, admin: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)):
    """Clear a needs_review purchase after checking the exchange:

    - record_fill: the exchange shows the order FILLED and the fill passes the
      guard -> recorded as the buy (never re-bought), schedule resumes, and for
      withdraw-immediately purchases the withdrawal is sent now;
    - mark_not_sent: the exchange shows NOT FOUND or CANCELLED -> the attempt is
      marked failed and the purchase is due again (a new buy, after the usual
      reconciliation);
    - cancel: stop the purchase; the attempt stays on record.
    Every resolution is audited with the admin's note."""
    import asyncio
    from api.tasks import purchase_scheduler as ps
    p = await _load_purchase(db, purchase_id)
    if p.status != "needs_review":
        raise HTTPException(status_code=409, detail=f"purchase is {p.status}, not needs_review")
    parked = sorted((t for t in p.transactions if t.status == "needs_review"), key=lambda t: t.created_at)
    if not parked:
        raise HTTPException(status_code=409, detail="no parked attempt on this purchase")
    tx = next((t for t in parked if payload.attempt_id and t.id == payload.attempt_id), parked[-1])
    now = datetime.now(timezone.utc)
    lookups = {l.attempt_id: l for l in await _lookup_parked(p)}
    look = lookups.get(tx.id)
    if payload.action == "cancel":
        p.status = "cancelled"
        p.next_execution = None
        p.last_error = f"cancelled by admin after review: {payload.note}"[:500]
        tx.error_message = f"{tx.error_message or ''} [admin: cancelled — {payload.note}]"[:500]
    elif payload.action == "record_fill":
        if look is None or look.state != "filled":
            raise HTTPException(status_code=409, detail=f"the exchange reports the order as {look.state if look else 'unknown'}"
                                                        f"{': ' + look.error if look and look.error else ''}; only a FILLED order can be recorded")
        if look.verdict != "ok":
            raise HTTPException(status_code=409, detail=f"the fill does not pass the guard: {look.verdict}")
        if p.purchase_type == "autobuy" and not payload.keep_on_exchange:
            withdraw_gate.require_owner_allowed_for_admin(p.user)   # the OWNER must be allowed, never the admin
        ps._record_fill(tx, look.fill, now)
        p.status = "active"
        ps._advance(p, now)
        kept = p.purchase_type == "autobuy" and payload.keep_on_exchange
        if kept:
            p.purchase_type = "dca"   # coins stay on the exchange; the owner may switch back where allowed
        tx.error_message = (f"{tx.error_message or ''} [admin: fill recorded"
                            f"{'; coins kept on the exchange, purchase switched to accumulate' if kept else ''} — {payload.note}]")[:500]
        await db.commit()
        if p.purchase_type == "autobuy" and p.exchange_account is not None:
            events: list = []
            await ps._withdraw_after_buy(db, p, p.exchange_account, tx, _gateway(), now, settings.autobuy_dry_run, events)
    else:  # mark_not_sent
        if look is None or look.state not in ("not_found", "cancelled"):
            raise HTTPException(status_code=409, detail=f"the exchange reports the order as {look.state if look else 'unknown'}"
                                                        f"{': ' + look.error if look and look.error else ''}; only NOT FOUND or CANCELLED can be marked not sent")
        tx.status = "failed"
        tx.raw_status = look.state
        tx.error_message = f"{tx.error_message or ''} [admin: not sent ({look.state}) — {payload.note}]"[:500]
        p.status = "active"
        p.failure_count = 0
        p.last_error = None
        p.next_execution = now
    await db.commit()
    audit(admin, "purchase_review_resolved", purchase_id=str(p.id), attempt_id=str(tx.id), resolution=payload.action,
          kept_on_exchange=bool(payload.keep_on_exchange), exchange_state=look.state if look else None, note=payload.note)
    return _purchase_row(await _load_purchase(db, purchase_id))


@router.post("/purchases/{purchase_id}/force-retry", status_code=204)
async def force_retry(
    purchase_id: UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Make an Auto-Buy purchase due immediately (the next execute_purchases
    run picks it up). A failed purchase is re-armed: status back to active and
    the attempt counter reset. Refused for withdrawal_failed / needs_info:
    those buys already filled and re-arming would buy again — the owner (or
    an admin acting as them) must use retry-withdrawal instead."""
    p = await purchase_or_404(db, purchase_id, eager=False)
    if p.status in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail=f"purchase is {p.status}")
    if p.exchange_account_id is None:
        raise HTTPException(status_code=409, detail="Exchange key removed: the purchase cannot run again; "
                                                    "the user must add a key and create a new purchase")
    if p.status in ("withdrawal_failed", "needs_info"):
        raise HTTPException(status_code=409, detail=f"purchase is {p.status}: its buy filled and the coins are on the "
                                                    "exchange; retry the withdrawal (POST /api/v2/purchases/{id}/retry-withdrawal), never the buy")
    if p.status == "needs_review":
        raise HTTPException(status_code=409, detail="purchase is needs_review: its buy outcome is unknown, re-arming it could buy "
                                                    "twice; inspect GET /api/admin/purchases/{id}/review and POST /resolve instead")
    if p.purchase_type == "autobuy":
        owner = (await db.execute(select(User).where(User.id == p.user_id))).scalar_one()
        withdraw_gate.require_owner_allowed_for_admin(owner)   # re-arming would withdraw on the next run
    p.status = "active"
    p.failure_count = 0
    p.next_execution = datetime.now(timezone.utc)
    await db.commit()
    audit(admin, "purchase_force_retry", purchase_id=str(purchase_id))


class FillDeviation(BaseModel):
    purchase_id: str
    user_id: str
    coin: str
    deviation_pct: float
    expected_amount: float
    actual_amount: float
    requested_usd: float
    cost_usd: float
    price: Optional[float] = None
    at: float


@router.get("/fills/deviations", response_model=List[FillDeviation])
async def fill_deviations(limit: int = Query(50, ge=1, le=100), admin: User = Depends(require_admin)):
    """Recent successful buys whose fill deviated beyond FILL_WARN_PCT (newest first)."""
    from api.services import fill_watch
    return [FillDeviation(**d) for d in await fill_watch.recent(limit)]

