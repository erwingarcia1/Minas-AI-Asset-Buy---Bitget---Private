"""Authentication routes: register, login, verify, refresh."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Response
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Token, create_access_token, hash_password, verify_password, verify_token
from api.config import settings
from api.database import get_db
from api.models import User
from api.routes.user import UTCModel, get_current_user
from fastapi import Request
from api.services.rate_limit import limiter, is_locked_out, register_failed_login, clear_failed_logins

import logging
logger = logging.getLogger("autobuy.auth")

router = APIRouter()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "letmein123", "admin123", "welcome123", "iloveyou1", "sunshine1",
}


def validate_password_strength(value: str) -> str:
    problems = []
    if not any(c.islower() for c in value):
        problems.append("a lowercase letter")
    if not any(c.isupper() for c in value):
        problems.append("an uppercase letter")
    if not any(c.isdigit() for c in value):
        problems.append("a digit")
    if not any(not c.isalnum() for c in value):
        problems.append("a special character")
    if problems:
        raise ValueError("password must contain " + ", ".join(problems))
    # normalize away case and decoration so "Password123!" still matches
    base = "".join(c for c in value.lower() if c.isalnum())
    if value.lower() in COMMON_PASSWORDS or base in COMMON_PASSWORDS:
        raise ValueError("password is too common")
    return value


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    _strength = field_validator("password")(validate_password_strength)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(UTCModel):
    id: UUID
    email: EmailStr
    kyc_status: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class VerifyRequest(BaseModel):
    token: str


class VerifyResponse(BaseModel):
    valid: bool
    user_id: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _create_refresh_token(user_id: str) -> str:
    """Create a long-lived refresh token (type=refresh, revocable via jti)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expire_days),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


REVOKED_PREFIX = "revoked-refresh:"


async def _is_revoked(jti: str) -> bool:
    from api.services.redis_client import get_redis
    try:
        return bool(await get_redis().exists(REVOKED_PREFIX + jti))
    except Exception:
        logger.warning("revocation check unavailable (Redis down); allowing token")
        return False


async def _revoke(jti: str, exp_ts: int) -> None:
    from api.services.redis_client import get_redis
    import time as _time
    ttl = max(int(exp_ts - _time.time()), 1)
    await get_redis().setex(REVOKED_PREFIX + jti, ttl, "1")


def _token_pair(user_id: str) -> TokenResponse:
    access: Token = create_access_token(user_id)
    return TokenResponse(
        access_token=access.access_token,
        refresh_token=_create_refresh_token(user_id),
        expires_in=access.expires_in,
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, response: Response, payload: RegisterRequest,
                   background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Register a new user with email and password."""
    email = payload.email.lower()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    from api.services.email_campaigns import send_welcome
    background.add_task(send_welcome, user.email)
    return user


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, response: Response, payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate with email/password and receive access + refresh tokens."""
    locked_for = await is_locked_out(payload.email)
    if locked_for:
        raise HTTPException(
            status_code=429,
            detail=f"Account locked after repeated failed logins. Try again in {locked_for // 60 + 1} minutes.",
            headers={"Retry-After": str(locked_for)},
        )
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        logger.warning("failed login attempt", extra={"email": payload.email.lower()})
        await register_failed_login(payload.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await clear_failed_logins(payload.email)
    return _token_pair(str(user.id))


@router.post("/verify", response_model=VerifyResponse)
@limiter.limit("10/minute")
async def verify(request: Request, response: Response, payload: VerifyRequest):
    """Verify an access token; returns validity and the user id it belongs to."""
    data = verify_token(payload.token)
    if data is None or data.user_id is None:
        return VerifyResponse(valid=False)
    return VerifyResponse(valid=True, user_id=data.user_id)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("10/minute")
async def refresh(request: Request, response: Response, payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a valid refresh token for a new access + refresh token pair."""
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        decoded = jwt.decode(
            payload.refresh_token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except JWTError:
        raise invalid
    if decoded.get("type") != "refresh" or not decoded.get("sub"):
        raise invalid
    try:
        user_id = UUID(decoded["sub"])
    except (ValueError, TypeError):
        raise invalid

    jti = decoded.get("jti")
    if jti and await _is_revoked(jti):
        raise invalid

    # Make sure the user still exists before minting new tokens.
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise invalid
    if user.password_changed_at is not None and decoded.get("iat") is not None:
        issued = datetime.fromtimestamp(decoded["iat"], tz=timezone.utc)
        if issued < user.password_changed_at:
            raise invalid
    return _token_pair(str(user_id))


class LogoutRequest(BaseModel):
    refresh_token: str


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def logout(request: Request, response: Response, payload: LogoutRequest):
    """Revoke a refresh token. The short-lived access token simply expires."""
    try:
        decoded = jwt.decode(
            payload.refresh_token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if decoded.get("type") != "refresh" or not decoded.get("jti"):
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    await _revoke(decoded["jti"], int(decoded.get("exp", 0)))


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    _strength = field_validator("new_password")(validate_password_strength)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the password and invalidate every token issued before now."""
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    current_user.password_hash = hash_password(payload.new_password)
    current_user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("password changed", extra={"user_id": str(current_user.id)})
