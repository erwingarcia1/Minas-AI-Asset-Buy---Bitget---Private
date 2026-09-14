"""Admin surface: platform stats, user management, transaction audit, alert
snapshot, and operational controls. Every route requires role=admin; every
state-changing action writes an audit log line with the acting admin's id."""
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

logger = logging.getLogger("autobuy.admin")

router = APIRouter()

VALID_KYC = {"pending", "verified", "rejected"}


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def audit(admin: User, action: str, **details) -> None:
    logger.info("admin_action", extra={"admin_id": str(admin.id),
                                       "action": action, **details})


# ------------------------------------------------------------------- stats

class PlatformStats(BaseModel):
    total_users: int
    verified_users: int
    suspended_users: int
    total_invested_usdt: float
    total_transactions: int
    failed_transactions_24h: int
    active_purchases: int
    signups_7d: int
    volume_7d_usdt: float


@router.get("/stats", response_model=PlatformStats)
async def platform_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide totals and 7-day trend inputs."""
    from api.services import history
    now = datetime.now(timezone.utc)
    day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
    row = (await db.execute(select(
        func.count(User.id),
        func.count(User.id).filter(User.kyc_status == "verified"),
        func.count(User.id).filter(User.suspended.is_(True)),
        func.count(User.id).filter(User.created_at >= week_ago),
    ))).one()
    totals = await history.platform_totals(db, day_ago, week_ago)
    active = (await db.execute(select(func.count(Purchase.id)).where(
        Purchase.status == "active"))).scalar_one()
    return PlatformStats(
        total_users=row[0], verified_users=row[1], suspended_users=row[2],
        signups_7d=row[3], active_purchases=active, **totals)


# ------------------------------------------------------------------- users

class AdminUserRow(UTCModel):
    id: UUID
    email: str
    role: str
    kyc_status: str
    suspended: bool
    created_at: datetime
    invested_usdt: float = 0.0
    wallet_hold_until: Optional[datetime] = None   # active 24h hold on new withdrawal addresses, else null
    wallet_hold_reason: Optional[str] = None


class AdminUserList(BaseModel):
    items: List[AdminUserRow]
    total: int


@router.get("/users", response_model=AdminUserList)
async def list_users(
    q: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """All users, searchable by email substring, newest first."""
    filters = []
    if q:
        filters.append(User.email.ilike(f"%{q}%"))
    total = (await db.execute(select(func.count(User.id)).where(*filters))).scalar_one()
    from api.services import history
    users = (await db.execute(
        select(User).where(*filters)
        .order_by(User.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    invested = await history.invested_per_user(db)
    items = []
    from api.services.wallet_hold import active_hold
    for user in users:
        row = AdminUserRow.model_validate(user)
        row.invested_usdt = invested.get(user.id, 0.0)
        row.wallet_hold_until = active_hold(user)
        row.wallet_hold_reason = user.wallet_hold_reason if row.wallet_hold_until else None
        items.append(row)
    return AdminUserList(items=items, total=total)


@router.post("/users/{user_id}/suspend", status_code=204)
async def suspend_user(
    user_id: UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Suspend: blocks login/API access and purchase execution immediately."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot suspend yourself")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.suspended = True
    await db.commit()
    audit(admin, "user_suspended", user_id=str(user_id))


@router.post("/users/{user_id}/unsuspend", status_code=204)
async def unsuspend_user(
    user_id: UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reactivate a suspended account."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.suspended = False
    await db.commit()
    audit(admin, "user_unsuspended", user_id=str(user_id))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    confirm: str = Query(..., description="must equal the user's email"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a user and (by cascade) their keys, schedules, and
    transactions. `confirm` must match the account email exactly."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if confirm != user.email:
        raise HTTPException(status_code=400,
                            detail="confirm must match the user's email exactly")
    await db.delete(user)
    await db.commit()
    audit(admin, "user_deleted", user_id=str(user_id), email=user.email)


class LiftWalletHold(BaseModel):
    confirm_email: str = Field(min_length=3, max_length=255)   # must equal the user's email
    reason: str = Field(min_length=5, max_length=500)          # how the customer proved who they are


@router.post("/users/{user_id}/lift-wallet-hold", status_code=204)
async def lift_wallet_hold(
    user_id: UUID,
    payload: LiftWalletHold,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Support override: clear the 24-hour wallet-change hold for a customer who
    reset their own password and proved their identity. Requires the user's
    email typed exactly and a reason; writes an audit entry (admin, when, why)
    and emails the user that the hold was lifted. 409 when no hold is active."""
    from api.services import wallet_hold
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.confirm_email.strip().lower() != user.email.lower():
        raise HTTPException(status_code=400, detail="confirm_email must match the user's email exactly")
    until = wallet_hold.active_hold(user)
    if until is None:
        raise HTTPException(status_code=409, detail="No wallet-change hold is active for this user")
    lifted_at = datetime.now(timezone.utc)
    user.wallet_hold_until = None
    user.wallet_hold_reason = None
    await db.commit()
    audit(admin, "wallet_hold_lifted", user_id=str(user.id), email=user.email, reason=payload.reason,
          hold_was_until=until.isoformat(), lifted_at=lifted_at.isoformat())
    await wallet_hold.notify_hold_lifted(db, user, admin, payload.reason, until, lifted_at)


# ------------------------------------------------------------------ parked purchases (needs_review)

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
    from sqlalchemy.orm import selectinload
    p = (await db.execute(select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.user),
                                                    selectinload(Purchase.exchange_account))
                          .execution_options(populate_existing=True).where(Purchase.id == purchase_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
    return p


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


def _gateway():
    from api.tasks.purchase_scheduler import ExchangeClientGateway
    return ExchangeClientGateway()


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
        ps._record_fill(tx, look.fill, now)
        p.status = "active"
        ps._advance(p, now)
        tx.error_message = f"{tx.error_message or ''} [admin: fill recorded — {payload.note}]"[:500]
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
          exchange_state=look.state if look else None, note=payload.note)
    return _purchase_row(await _load_purchase(db, purchase_id))


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


class KycOverride(BaseModel):
    status: str
    note: str = Field(min_length=5, max_length=500)


@router.post("/users/{user_id}/kyc-override", status_code=204)
async def kyc_override(
    user_id: UUID,
    payload: KycOverride,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manual KYC decision (support escalations). The note is mandatory and
    lands in the audit log."""
    if payload.status not in VALID_KYC:
        raise HTTPException(status_code=422,
                            detail=f"status must be one of {sorted(VALID_KYC)}")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.kyc_status = payload.status
    await db.commit()
    audit(admin, "kyc_override", user_id=str(user_id),
          new_status=payload.status, note=payload.note)


# ------------------------------------------------------------- transactions

class AdminTransactionRow(UTCModel):
    id: UUID
    user_id: UUID
    crypto: str
    cost_usdt: float
    status: str
    error_message: Optional[str] = None
    created_at: datetime


class AdminTransactionList(BaseModel):
    items: List[AdminTransactionRow]
    total: int


@router.get("/transactions", response_model=AdminTransactionList)
async def all_transactions(
    status_filter: Optional[str] = Query(None),
    user_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Every user's Auto-Buy execution attempts (purchase_transactions), newest
    first; filter by status (pending | completed | failed) and/or user."""
    from api.services import history
    if status_filter is not None and status_filter not in history.API_STATUSES:
        raise HTTPException(status_code=422, detail=f"status_filter must be one of {history.API_STATUSES}")
    rows, total = await history.list_rows(db, user_id, status=status_filter, limit=limit, offset=offset)
    return AdminTransactionList(
        items=[AdminTransactionRow(id=r.id, user_id=r.user_id, crypto=r.crypto, cost_usdt=r.cost_usd,
                                   status=r.status, error_message=r.error_message, created_at=r.created_at)
               for r in rows],
        total=total)


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
    p = (await db.execute(select(Purchase).where(Purchase.id == purchase_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
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
    p.status = "active"
    p.failure_count = 0
    p.next_execution = datetime.now(timezone.utc)
    await db.commit()
    audit(admin, "purchase_force_retry", purchase_id=str(purchase_id))


# ------------------------------------------------------------------ alerts

class UndeliveredEmail(BaseModel):
    id: Optional[str] = None
    to: str
    subject: str
    template: Optional[str] = None
    category: Optional[str] = None
    user_id: Optional[str] = None
    attempts: int = 0
    last_error: Optional[str] = None
    queued_at: Optional[float] = None
    gave_up_at: Optional[float] = None


class EmailOutboxView(BaseModel):
    queued: int                               # messages waiting or scheduled for retry
    failures_this_hour: Optional[int] = None  # give-ups in the current UTC hour
    alert_threshold: int
    pending: List[dict]                       # queued messages with attempts / next_attempt_at (no bodies)
    undelivered: List[UndeliveredEmail]       # gave up after all retries, newest first


@router.get("/emails/undelivered", response_model=EmailOutboxView)
async def undelivered_emails(limit: int = Query(100, ge=1, le=500), admin: User = Depends(require_admin)):
    """What the email outbox could not deliver (and what is still retrying)."""
    from api.services import email_outbox
    return EmailOutboxView(queued=await email_outbox.length() or 0,
                           failures_this_hour=await email_outbox.failures_this_hour(),
                           alert_threshold=email_outbox.failure_alert_threshold(),
                           pending=await email_outbox.pending(limit),
                           undelivered=[UndeliveredEmail(**u) for u in await email_outbox.undelivered(limit)])


@router.get("/alerts", response_model=List[AlertItem])
async def alerts(admin: User = Depends(require_admin),
                 db: AsyncSession = Depends(get_db)):
    """Live evaluation of the docs/ALERTING.md conditions (see api/services/admin_alerts.py)."""
    return await compute_alerts(db)


# ------------------------------------------------------------------ control

@router.post("/scheduler/pause", status_code=204)
async def pause_scheduler(admin: User = Depends(require_admin)):
    """Emergency stop for BOTH schedulers (legacy recurring tick and the
    Auto-Buy execute_purchases job) in every worker. Sets a switch in Redis
    that each run checks first; it survives API restarts until resumed.
    While paused, runs log that they were skipped and report
    ``skipped_paused`` in /health."""
    from api.services import scheduler_state
    info = await scheduler_state.pause(str(admin.id))
    audit(admin, "schedulers_paused", since=info["since"])


@router.post("/scheduler/resume", status_code=204)
async def resume_scheduler(admin: User = Depends(require_admin)):
    """Clear the pause switch; both schedulers execute again from their next run."""
    from api.services import scheduler_state
    await scheduler_state.resume()
    audit(admin, "schedulers_resumed")
