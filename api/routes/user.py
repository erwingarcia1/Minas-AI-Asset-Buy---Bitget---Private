"""User profile routes, plus shared auth dependency and response-model base.

`get_current_user` and `UTCModel` are defined here and imported by the other
route modules.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.routes._deps import user_or_404
from api.models import User

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
    if payload.get("type") in ("refresh", "email_verify", "password_reset"):  # only access tokens log in
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
    if user.password_changed_at is not None and payload.get("iat") is not None:
        issued = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
        if issued < user.password_changed_at:
            raise unauthorized  # token predates the last password change
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


class UserPublicProfile(UTCModel):
    id: UUID
    email: EmailStr
    kyc_status: str
    created_at: datetime


class ProfileUpdateRequest(BaseModel):
    # kyc_status is intentionally absent: it is owned by the KYC flow
    # (api/routes/kyc.py) and must never be user-writable.
    email: Optional[EmailStr] = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _profile(user: User) -> UserProfile:
    from api.services.wallet_hold import active_hold
    out = UserProfile.model_validate(user, from_attributes=True)
    until = active_hold(user)
    out.wallet_hold_until = until
    out.wallet_hold_reason = user.wallet_hold_reason if until else None
    return out


@router.get("/profile", response_model=UserProfile)
async def get_profile(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's own profile, including any active
    wallet-change hold (`wallet_hold_until`)."""
    return _profile(current_user)


@router.put("/profile", response_model=UserProfile)
async def update_profile(
    payload: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the authenticated user's email and/or KYC status."""
    if payload.email is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    if payload.email is not None:
        new_email = payload.email.lower()
        if new_email != current_user.email:
            existing = await db.execute(select(User).where(User.email == new_email))
            if existing.scalar_one_or_none() is not None:
                raise HTTPException(status_code=400, detail="Email already in use")
            current_user.email = new_email

    current_user.updated_at = to_db_datetime(utc_now())
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.get("/{user_id}", response_model=UserPublicProfile)
async def get_user_by_id(
    user_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get any user's public profile by ID (requires authentication)."""
    return await user_or_404(db, user_id)
