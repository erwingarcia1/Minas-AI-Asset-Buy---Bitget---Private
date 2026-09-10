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

import hashlib
import logging

from api.integrations import resend
from api.integrations.resend import ResendError

logger = logging.getLogger("autobuy.auth")

PASSWORD_RESET_TTL = timedelta(hours=1)
RESET_USED_PREFIX = "pwreset-used:"

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


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    sent: bool = True


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=2048)
    new_password: str = Field(min_length=8, max_length=128)

    _strength = field_validator("new_password")(validate_password_strength)


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


# --------------------------------------------------------------------------
# Password reset (forgot password)
# --------------------------------------------------------------------------

def _password_version(password_hash: str) -> str:
    """Fingerprint of the current hash: a reset token embeds it, so the token
    dies the moment the password changes (single use, and useless after a
    normal password change)."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_password_reset_token(user: User) -> str:
    """One-hour JWT (type=password_reset) bound to the user's current password.
    Rejected by the bearer guard, so a leaked link cannot log anyone in."""
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user.id), "type": "password_reset", "jti": uuid4().hex,
                       "pv": _password_version(user.password_hash),
                       "iat": now, "exp": now + PASSWORD_RESET_TTL},
                      settings.secret_key, algorithm=settings.algorithm)


def password_reset_link(token: str) -> str:
    base = (settings.app_base_url or (settings.allowed_origins or ["http://localhost:3000"])[0]).rstrip("/")
    return f"{base}/reset-password?token={token}"


async def _send_password_reset_email(email: str, link: str) -> None:
    text = (f"We received a request to reset the password for your AutoBuy account.\n\n"
            f"Open this link within 1 hour to choose a new password:\n{link}\n\n"
            "If you did not request this, you can ignore this email; your password will not change.")
    html = (f"<p>We received a request to reset the password for your AutoBuy account.</p>"
            f"<p><a href=\"{link}\">Reset my password</a> (valid for 1 hour)</p>"
            "<p>If you did not request this, you can ignore this email; your password will not change.</p>")
    try:
        await resend.send_email(email, "Reset your AutoBuy password", html, text_content=text, template="password_reset")
    except ResendError as exc:
        logger.warning("password reset email not delivered to %s: %s", email, exc)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
@limiter.limit("3/minute")
async def forgot_password(request: Request, response: Response, payload: ForgotPasswordRequest,
                          background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Email a one-hour password reset link. Always answers `{sent: true}`
    so the response cannot be used to discover which emails have accounts."""
    email = payload.email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and not user.suspended:
        link = password_reset_link(make_password_reset_token(user))
        background.add_task(_send_password_reset_email, user.email, link)
        logger.info("password reset requested", extra={"user_id": str(user.id)})
        if settings.api_env != "production":
            logger.info("password reset link (non-production only): %s", link)
    else:
        logger.info("password reset requested for unknown or suspended email")
    return ForgotPasswordResponse(sent=True)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
@limiter.limit("5/minute")
async def reset_password(request: Request, response: Response, payload: ResetPasswordRequest,
                         db: AsyncSession = Depends(get_db)):
    """Set a new password using a reset token from `/forgot-password`.
    The token is single-use and expires after an hour; on success every
    existing session is signed out."""
    invalid = HTTPException(status_code=400, detail="Invalid or expired reset link")
    try:
        decoded = jwt.decode(payload.token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        raise invalid
    if decoded.get("type") != "password_reset" or not decoded.get("sub") or not decoded.get("jti"):
        raise invalid
    try:
        user_id = UUID(decoded["sub"])
    except (ValueError, TypeError):
        raise invalid
    if await _is_reset_used(decoded["jti"]):
        raise invalid
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or user.suspended or decoded.get("pv") != _password_version(user.password_hash):
        raise invalid
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=422, detail="New password must differ from the current password")

    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
    await _mark_reset_used(decoded["jti"], int(decoded.get("exp", 0)))
    logger.info("password reset completed", extra={"user_id": str(user.id)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _is_reset_used(jti: str) -> bool:
    from api.services.redis_client import get_redis
    try:
        return bool(await get_redis().exists(RESET_USED_PREFIX + jti))
    except Exception:
        return False  # the password-version check still prevents reuse


async def _mark_reset_used(jti: str, exp_ts: int) -> None:
    from api.services.redis_client import get_redis
    try:
        ttl = max(exp_ts - int(datetime.now(timezone.utc).timestamp()), 60)
        await get_redis().setex(RESET_USED_PREFIX + jti, ttl, "1")
    except Exception:
        logger.warning("could not record used reset token (Redis down)")
