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
from api.models import Purchase, User
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
    p.status = "active"
    p.failure_count = 0
    p.next_execution = datetime.now(timezone.utc)
    await db.commit()
    audit(admin, "purchase_force_retry", purchase_id=str(purchase_id))


# ------------------------------------------------------------------ alerts

class AlertItem(BaseModel):
    name: str
    severity: str  # ok | warning | critical
    value: str
    detail: str


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _severity_for_failures(fails: Optional[int]) -> str:
    if fails is None:
        return "warning"
    return "critical" if fails >= 5 else "warning" if fails >= 2 else "ok"


@router.get("/alerts", response_model=List[AlertItem])
async def alerts(admin: User = Depends(require_admin),
                 db: AsyncSession = Depends(get_db)):
    """Live evaluation of the docs/ALERTING.md conditions the app can see for
    the Auto-Buy execute_purchases job. Heartbeats come from Redis, so they
    describe whichever worker actually ran, not the one answering."""
    from api.services import scheduler_state
    from api.tasks.purchase_scheduler import INTERVAL_MINUTES, MISFIRE_GRACE_SECONDS

    now = datetime.now(timezone.utc)
    items = []

    # -- pause switch (both schedulers) --
    try:
        pause = await scheduler_state.pause_info()
        items.append(AlertItem(
            name="schedulers_paused",
            severity="warning" if pause["paused"] else "ok",
            value="paused" if pause["paused"] else "running",
            detail=("Admin pause switch for the Auto-Buy job"
                    + (f"; paused by {pause['by']} since {pause['since']}" if pause["paused"] else ""))))
    except scheduler_state.PauseStateUnavailable:
        pause = {"paused": None}
        items.append(AlertItem(
            name="schedulers_paused", severity="critical", value="unknown",
            detail="Redis unreadable: the Auto-Buy job fails closed and skips every run until it is back"))

    # -- Auto-Buy execute_purchases job --
    ab = await scheduler_state.status("autobuy")
    ab_fails = ab["consecutive_failures"]
    items.append(AlertItem(
        name="autobuy_consecutive_failures",
        severity=_severity_for_failures(ab_fails),
        value="unknown" if ab_fails is None else str(ab_fails),
        detail="Auto-Buy job: consecutive runs that raised or failed every due purchase (critical at 5)"))

    overdue_after = timedelta(minutes=INTERVAL_MINUTES * 2, seconds=MISFIRE_GRACE_SECONDS)
    ab_last = _parse_iso(ab["last_run_at"])
    if ab_last is None:
        ab_sev = "warning"
    elif now - ab_last > overdue_after:
        ab_sev = "critical"
    elif ab["last_result"] in ("error", "failed"):
        ab_sev = "warning"
    else:
        ab_sev = "ok"
    items.append(AlertItem(
        name="autobuy_run",
        severity=ab_sev,
        value=ab["last_run_at"] or "never",
        detail=(f"Auto-Buy job: last execute_purchases run (every {INTERVAL_MINUTES} min; "
                f"critical if silent >{int(overdue_after.total_seconds() // 60)} min)"
                + (f"; last result {ab['last_result']}" if ab["last_result"] else "")
                + (f" {ab['last_counts']}" if ab["last_counts"] else ""))))

    # -- failed Auto-Buy execution attempts in the last 24h (purchase_transactions) --
    from api.services import history
    failed_24h = await history.count_failed_since(db, now - timedelta(days=1))
    items.append(AlertItem(
        name="failed_transactions_24h",
        severity="warning" if failed_24h > 10 else "ok",
        value=str(failed_24h), detail="Auto-Buy job: failed execution attempts in the last 24h"))

    return items


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
