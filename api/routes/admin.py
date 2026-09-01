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
from api.models import RecurringPurchase, Transaction, User
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
    now = datetime.now(timezone.utc)
    day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
    completed = Transaction.status == "completed"
    row = (await db.execute(select(
        func.count(User.id),
        func.count(User.id).filter(User.kyc_status == "verified"),
        func.count(User.id).filter(User.suspended.is_(True)),
        func.count(User.id).filter(User.created_at >= week_ago),
    ))).one()
    tx = (await db.execute(select(
        func.coalesce(func.sum(Transaction.cost_usdt).filter(completed), 0.0),
        func.count(Transaction.id),
        func.count(Transaction.id).filter(Transaction.status == "failed",
                                          Transaction.created_at >= day_ago),
        func.coalesce(func.sum(Transaction.cost_usdt).filter(
            completed, Transaction.created_at >= week_ago), 0.0),
    ))).one()
    active = (await db.execute(select(func.count(RecurringPurchase.id)).where(
        RecurringPurchase.enabled.is_(True)))).scalar_one()
    return PlatformStats(
        total_users=row[0], verified_users=row[1], suspended_users=row[2],
        signups_7d=row[3], total_invested_usdt=round(float(tx[0]), 2),
        total_transactions=tx[1], failed_transactions_24h=tx[2],
        volume_7d_usdt=round(float(tx[3]), 2), active_purchases=active)


# ------------------------------------------------------------------- users

class AdminUserRow(UTCModel):
    id: UUID
    email: str
    role: str
    kyc_status: str
    suspended: bool
    created_at: datetime
    invested_usdt: float = 0.0


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
    rows = (await db.execute(
        select(User, func.coalesce(func.sum(Transaction.cost_usdt).filter(
            Transaction.status == "completed"), 0.0))
        .outerjoin(Transaction, Transaction.user_id == User.id)
        .where(*filters).group_by(User.id)
        .order_by(User.created_at.desc()).limit(limit).offset(offset))).all()
    items = []
    for user, invested in rows:
        row = AdminUserRow.model_validate(user)
        row.invested_usdt = round(float(invested), 2)
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
    """Cross-user transaction audit log, newest first."""
    filters = []
    if status_filter:
        filters.append(Transaction.status == status_filter)
    if user_id:
        filters.append(Transaction.user_id == user_id)
    total = (await db.execute(
        select(func.count(Transaction.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(Transaction).where(*filters)
        .order_by(Transaction.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return AdminTransactionList(
        items=[AdminTransactionRow.model_validate(t) for t in rows], total=total)


@router.post("/purchases/{purchase_id}/force-retry", status_code=204)
async def force_retry(
    purchase_id: UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Make a schedule due immediately (next scheduler tick executes it)."""
    p = (await db.execute(select(RecurringPurchase).where(
        RecurringPurchase.id == purchase_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
    p.next_purchase_date = datetime.now(timezone.utc)
    await db.commit()
    audit(admin, "purchase_force_retry", purchase_id=str(purchase_id))


# ------------------------------------------------------------------ alerts

class AlertItem(BaseModel):
    name: str
    severity: str  # ok | warning | critical
    value: str
    detail: str


@router.get("/alerts", response_model=List[AlertItem])
async def alerts(admin: User = Depends(require_admin),
                 db: AsyncSession = Depends(get_db)):
    """Live evaluation of the docs/ALERTING.md conditions the app can see."""
    from api.services.metrics import (
        active_purchases_total, scheduler_consecutive_failures)
    from api.services.recurring_scheduler import scheduler

    items = []
    fails = int(scheduler_consecutive_failures._value.get())
    items.append(AlertItem(
        name="scheduler_consecutive_failures",
        severity="critical" if fails >= 5 else "warning" if fails >= 2 else "ok",
        value=str(fails),
        detail="Consecutive failed purchase executions (critical at 5)"))

    last = scheduler.last_tick_at
    stalled = (last is None or
               datetime.now(timezone.utc) - last > timedelta(minutes=5))
    items.append(AlertItem(
        name="scheduler_tick",
        severity=("critical" if scheduler.running and stalled and last is not None
                  else "warning" if not scheduler.running else "ok"),
        value=last.isoformat() if last else "never",
        detail="Last scheduler tick (critical if silent >5m while running)"))

    active = int(active_purchases_total._value.get())
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    failed_24h = (await db.execute(select(func.count(Transaction.id)).where(
        Transaction.status == "failed",
        Transaction.created_at >= day_ago))).scalar_one()
    items.append(AlertItem(
        name="failed_transactions_24h",
        severity="warning" if failed_24h > 10 else "ok",
        value=str(failed_24h), detail="Failed executions in the last 24h"))
    items.append(AlertItem(
        name="active_purchases", severity="ok", value=str(active),
        detail="Enabled schedules at last tick"))
    return items


# ------------------------------------------------------------------ control

@router.post("/scheduler/pause", status_code=204)
async def pause_scheduler(admin: User = Depends(require_admin)):
    """Emergency stop: no purchases execute until resumed (or restart)."""
    from api.services.recurring_scheduler import scheduler
    scheduler.stop()
    audit(admin, "scheduler_paused")


@router.post("/scheduler/resume", status_code=204)
async def resume_scheduler(admin: User = Depends(require_admin)):
    """Restart the paused scheduler."""
    from api.services.recurring_scheduler import scheduler
    if not scheduler.running:
        scheduler.start()
    audit(admin, "scheduler_resumed")
