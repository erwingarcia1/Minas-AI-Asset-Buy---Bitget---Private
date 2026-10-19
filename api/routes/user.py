"""User profile routes, plus shared auth dependency and response-model base.

`get_current_user` and `UTCModel` are defined here and imported by the other
route modules.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import token_cutoff, verify_password
from api.config import settings
from api.database import get_db
from api.routes._deps import user_or_404
from api.models import User
from api.services import twofactor
from api.services.email_service import email_service
from api.services.email_templates import render
from api.services.rate_limit import register_failed_login
from api.services.telegram import mask_email

logger = logging.getLogger("autobuy.user")

router = APIRouter()

bearer_scheme = HTTPBearer(auto_error=False)

VALID_KYC_STATUSES = {"pending", "verified", "rejected"}


class UTCModel(BaseModel):
    """Base response model: ORM mode + naive DB datetimes exposed as UTC-aware."""

    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="before")
    @classmethod
    def _make_datetimes_utc(cls, value):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def to_db_datetime(dt: datetime) -> datetime:
    """Normalize a datetime to aware UTC for storage (columns are timestamptz)."""
    return dt.astimezone(timezone.utc)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract the Bearer token, validate the JWT, and return the User row."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        payload = jwt.decode(
            credentials.credentials, settings.secret_key, algorithms=[settings.algorithm]
        )
    except JWTError:
        raise unauthorized
    if payload.get("type") in ("refresh", "email_verify", "password_reset", "mfa_pending"):  # only access tokens log in
        raise unauthorized
    sub = payload.get("sub")
    if not sub:
        raise unauthorized
    try:
        user_id = UUID(sub)
    except (ValueError, TypeError):
        raise unauthorized
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise unauthorized
    if user.suspended:
        raise HTTPException(status_code=403, detail="Account suspended — contact support")
    cutoff = token_cutoff(user)
    if cutoff is not None and payload.get("iat") is not None:
        issued = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
        if issued < cutoff:
            raise unauthorized  # token predates the last password change or "sign out everywhere"
    return user


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class UserProfile(UTCModel):
    id: UUID
    email: EmailStr
    role: str = "user"
    kyc_status: str
    kyc_sumsub_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # active 24h hold on new withdrawal addresses (after a password reset/change), else null
    wallet_hold_until: Optional[datetime] = None
    wallet_hold_reason: Optional[str] = None
    # private beta: may this user create/switch withdraw-immediately purchases? (server-enforced; the UI hides the controls)
    withdrawals_allowed: bool = False
    # Guardian (v0.9.2): may this user see the monitor? The flag and the allowlist, decided server-side.
    guardian_enabled: bool = False
    # two-factor authentication confirmed on this account
    mfa_enabled: bool = False


class UserPublicProfile(UTCModel):
    id: UUID
    email: EmailStr
    kyc_status: str
    created_at: datetime


class ProfileUpdateRequest(BaseModel):
    # kyc_status is intentionally absent: it is owned by the KYC flow
    # (api/routes/kyc.py) and must never be user-writable.
    email: Optional[EmailStr] = None
    # Changing the email address (where security notices and Guardian alerts go) needs the
    # current password, plus the current authenticator code when two-factor is on (v0.9.2).
    current_password: Optional[str] = Field(default=None, max_length=128)
    code: Optional[str] = Field(default=None, max_length=32)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _profile(user: User) -> UserProfile:
    from api.services.wallet_hold import active_hold
    out = UserProfile.model_validate(user, from_attributes=True)
    until = active_hold(user)
    out.wallet_hold_until = until
    out.wallet_hold_reason = user.wallet_hold_reason if until else None
    from api.services.withdraw_gate import withdrawals_allowed
    out.withdrawals_allowed = withdrawals_allowed(user)
    from api.services.guardian_access import guardian_visible   # lazy: that module depends on this one
    out.guardian_enabled = guardian_visible(user)
    return out


async def _full_profile(db: AsyncSession, user: User) -> UserProfile:
    """`_profile` plus what needs the database: the two-factor state."""
    out = _profile(user)
    out.mfa_enabled = await twofactor.is_enabled(db, user.id)
    return out


async def send_email_changed_notice(old_email: str, new_email: str, when: datetime) -> None:
    """Security notice to the address that was replaced. Never optional and
    never affects the change itself; queued through the outbox (security
    category). The new address appears masked."""
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    html, text = render("email_changed", new_email_masked=mask_email(new_email), when=stamp)
    subject = f"The email address on your {settings.app_name} account was changed"
    if not await email_service.queue(old_email, subject, html, text, template="email_changed", category="security"):
        logger.error("email change notice not delivered to %s: outbox unavailable and inline attempt failed",
                     mask_email(old_email))


@router.get("/profile", response_model=UserProfile)
async def get_profile(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return the authenticated user's own profile, including any active
    wallet-change hold (`wallet_hold_until`), whether two-factor authentication
    is on (`mfa_enabled`) and whether Guardian is available to this account
    (`guardian_enabled`)."""
    return await _full_profile(db, current_user)


@router.put("/profile", response_model=UserProfile)
async def update_profile(
    payload: ProfileUpdateRequest,
    background: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the authenticated user's email address. Security notices go to
    that address, so changing it needs `current_password`, plus the current
    authenticator `code` when two-factor authentication is on, and the
    previous address is told about the change. KYC status is owned by the KYC
    flow and is not writable here."""
    if payload.email is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    new_email = payload.email.lower()
    if new_email != current_user.email:
        old_email = current_user.email
        if not payload.current_password:
            raise HTTPException(status_code=400,
                                detail="Enter your current password to change the email address on this account.")
        if not verify_password(payload.current_password, current_user.password_hash):
            await register_failed_login(current_user.email)
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        existing = await db.execute(select(User).where(User.email == new_email))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=400, detail="Email already in use")
        if await twofactor.is_enabled(db, current_user.id):
            # check_code spends the 30-second step, so a code seen here cannot be replayed into a sign-in
            code = (payload.code or "").strip()
            if not code or not await twofactor.check_code(db, current_user, code):
                raise HTTPException(status_code=400,
                                    detail="Enter the current code from your authenticator app to change the email address.")
        changed_at = utc_now()
        current_user.email = new_email
        current_user.updated_at = to_db_datetime(changed_at)
        await db.commit()
        await db.refresh(current_user)
        logger.info("email_changed", extra={"user_id": str(current_user.id),
                                            "from": mask_email(old_email), "to": mask_email(new_email)})
        background.add_task(send_email_changed_notice, old_email, new_email, changed_at)
        return await _full_profile(db, current_user)

    current_user.updated_at = to_db_datetime(utc_now())
    await db.commit()
    await db.refresh(current_user)
    return await _full_profile(db, current_user)


@router.get("/{user_id}", response_model=UserPublicProfile)
async def get_user_by_id(
    user_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get any user's public profile by ID (requires authentication)."""
    return await user_or_404(db, user_id)
