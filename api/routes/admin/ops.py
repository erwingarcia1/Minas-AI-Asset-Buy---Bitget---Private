"""Admin routes — Operations: platform stats, transaction audit, undelivered emails, alerts, scheduler pause/resume.

Split out of api/routes/admin.py on 2026-09-22 (audit Batch B4); every route
still requires role=admin and every state change writes an audit line."""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.config import settings
from api.services.admin_alerts import AlertItem, compute_alerts
from api.models import Purchase, PurchaseTransaction, User
from api.routes.user import UTCModel, get_current_user

from api.routes.admin._deps import VALID_KYC, audit, logger, require_admin  # noqa: F401
from api.services.rate_limit import limiter, user_or_ip_key

router = APIRouter()


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


class TelegramTestResult(BaseModel):
    configured: bool
    sent: bool
    error: Optional[str] = None   # Telegram's description or a network error; never the token


@router.post("/telegram/test", response_model=TelegramTestResult)
@limiter.limit("3/minute", key_func=user_or_ip_key)
async def telegram_test(request: Request, response: Response, admin: User = Depends(require_admin)):
    """Send one test message to the configured Telegram chat and report the
    outcome, so the alert channel can be checked without a real buy."""
    from api.services import telegram
    if not telegram.configured():
        audit(admin, "telegram_test", sent=False, error="not configured")
        return TelegramTestResult(configured=False, sent=False, error="TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set")
    result = await telegram.send_detailed(
        f"🔔 {settings.app_name} test message from the admin console (admin {telegram.mask_email(admin.email)})", None, force=True)
    audit(admin, "telegram_test", sent=result.ok, error=result.error)
    return TelegramTestResult(configured=True, sent=result.ok, error=result.error)
