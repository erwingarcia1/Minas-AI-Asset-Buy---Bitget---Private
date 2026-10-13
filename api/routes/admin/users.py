"""Admin routes — User management: list, suspend, delete, wallet-hold override, KYC override.

Split out of api/routes/admin.py on 2026-09-22 (audit Batch B4); every route
still requires role=admin and every state change writes an audit line."""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.config import settings
from api.services.admin_alerts import AlertItem, compute_alerts
from api.models import Purchase, PurchaseTransaction, User
from api.routes.user import UTCModel, get_current_user

from api.routes._deps import user_or_404
from api.routes.admin._deps import VALID_KYC, audit, logger, require_admin  # noqa: F401
from api.services import withdraw_gate

router = APIRouter()


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
    totp_enabled: bool = False                     # two-factor authentication confirmed (v0.9.0)


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
    from api.models.security import UserTotp
    enrolled = set((await db.execute(
        select(UserTotp.user_id).where(UserTotp.confirmed.is_(True),
                                       UserTotp.user_id.in_([u.id for u in users])))).scalars().all()) if users else set()
    items = []
    from api.services.wallet_hold import active_hold
    for user in users:
        row = AdminUserRow.model_validate(user)
        row.invested_usdt = invested.get(user.id, 0.0)
        row.wallet_hold_until = active_hold(user)
        row.wallet_hold_reason = user.wallet_hold_reason if row.wallet_hold_until else None
        row.totp_enabled = user.id in enrolled
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
    user = await user_or_404(db, user_id)
    user.suspended = True
    await db.commit()
    audit(admin, "user_suspended", user_id=str(user_id))


class UnsuspendPayload(BaseModel):
    pause_withdraw_mode: bool = False   # private beta: pause the owner's active withdraw-mode purchases instead of refusing


@router.post("/users/{user_id}/unsuspend", status_code=204)
async def unsuspend_user(
    user_id: UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    payload: Optional[UnsuspendPayload] = None,
):
    """Reactivate a suspended account. During the private beta an account that
    owns ACTIVE withdraw-mode purchases and isn't allowed withdrawals is refused
    (403, the purchase ids listed), because the executor would run them again;
    send `{"pause_withdraw_mode": true}` to pause those purchases and reactivate
    the account (the owner can switch them to accumulate and resume)."""
    user = await user_or_404(db, user_id)
    paused: list = []
    if payload is not None and payload.pause_withdraw_mode:
        ids = await withdraw_gate.active_withdraw_mode_purchase_ids(db, user)
        if ids:
            rows = (await db.execute(select(Purchase).where(Purchase.id.in_(ids)))).scalars().all()
            for p in rows:
                p.status = "paused"
                p.last_error = f"{withdraw_gate.MESSAGE} {withdraw_gate.RESUME_HINT}"[:500]
                paused.append(str(p.id))
    else:
        await withdraw_gate.require_no_active_withdraw_mode(db, user, admin=True)
    user.suspended = False
    await db.commit()
    audit(admin, "user_unsuspended", user_id=str(user_id), paused_withdraw_mode_purchases=paused)


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
    user = await user_or_404(db, user_id)
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
    user = await user_or_404(db, user_id)
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
    user = await user_or_404(db, user_id)
    if payload.status == "verified" and user.kyc_status != "verified":
        await withdraw_gate.require_no_active_withdraw_mode(db, user, admin=True)   # verified = eligible to run again
    user.kyc_status = payload.status
    await db.commit()
    audit(admin, "kyc_override", user_id=str(user_id),
          new_status=payload.status, note=payload.note)


@router.post("/users/{user_id}/2fa/reset", status_code=204)
async def reset_two_factor(
    user_id: UUID,
    background: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Support path for a lost authenticator: remove the user's two-factor
    enrolment and recovery codes so they can sign in with the password alone
    and enrol again. Audited; the user is emailed when something was removed."""
    user = await user_or_404(db, user_id)
    from api.services import twofactor
    removed = await twofactor.admin_reset(db, user.id)
    audit(admin, "user_2fa_reset", user_id=str(user_id), had_enrolment=removed)
    if removed:
        from api.routes.twofactor import send_two_factor_notice
        background.add_task(send_two_factor_notice, user.email, "reset_by_admin", when=datetime.now(timezone.utc))
