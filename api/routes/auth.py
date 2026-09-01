"""Authentication routes: register, login, verify, refresh."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Token, create_access_token, hash_password, verify_password, verify_token
from api.config import settings
from api.database import get_db
from api.models import User
from api.routes.user import UTCModel

router = APIRouter()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


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
    """Create a long-lived refresh token (marked with type=refresh)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expire_days),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


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
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user with email and password."""
    email = payload.email.lower()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate with email/password and receive access + refresh tokens."""
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token_pair(str(user.id))


@router.post("/verify", response_model=VerifyResponse)
async def verify(payload: VerifyRequest):
    """Verify an access token; returns validity and the user id it belongs to."""
    data = verify_token(payload.token)
    if data is None or data.user_id is None:
        return VerifyResponse(valid=False)
    return VerifyResponse(valid=True, user_id=data.user_id)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
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

    # Make sure the user still exists before minting new tokens.
    result = await db.execute(select(User).where(User.id == user_id))
    if result.scalar_one_or_none() is None:
        raise invalid
    return _token_pair(str(user_id))
