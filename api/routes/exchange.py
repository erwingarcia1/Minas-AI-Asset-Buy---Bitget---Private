"""Exchange API key management with Fernet encryption at rest."""
import base64
import hashlib
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.models import APIKey, User
from api.routes.user import UTCModel, get_current_user, to_db_datetime, utc_now

router = APIRouter()

SUPPORTED_EXCHANGES = {"binance"}


# --------------------------------------------------------------------------
# Encryption helpers
# --------------------------------------------------------------------------

def _fernet() -> Fernet:
    """Fernet keyed off ENCRYPTION_KEY (falling back to SECRET_KEY in dev),
    sha256-derived to a urlsafe-b64 32-byte key."""
    material = settings.encryption_key or settings.secret_key
    digest = hashlib.sha256(material.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(value: str) -> str:
    """Encrypt a secret for storage."""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    """Decrypt a stored secret."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise HTTPException(
            status_code=500, detail="Stored API key cannot be decrypted"
        )


def mask_api_key(value: str) -> str:
    """Show only the first 4 and last 4 characters of a key."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class APIKeyCreate(BaseModel):
    exchange: str = Field(default="binance")
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=8, max_length=256)

    @field_validator("exchange")
    @classmethod
    def _supported(cls, value):
        value = value.lower()
        if value not in SUPPORTED_EXCHANGES:
            raise ValueError(
                f"exchange must be one of: {', '.join(sorted(SUPPORTED_EXCHANGES))}"
            )
        return value


class APIKeyResponse(UTCModel):
    id: UUID
    exchange: str
    api_key_masked: str
    verified: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _to_response(key: APIKey) -> APIKeyResponse:
    return APIKeyResponse(
        id=key.id,
        exchange=key.exchange,
        api_key_masked=mask_api_key(decrypt_value(key.encrypted_api_key)),
        verified=key.verified,
        created_at=key.created_at,
        updated_at=key.updated_at,
    )


@router.post("/keys", response_model=APIKeyResponse, status_code=status.HTTP_201_CREATED)
async def add_or_update_key(
    payload: APIKeyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a new API key for an exchange, or update the existing one."""
    result = await db.execute(
        select(APIKey).where(
            APIKey.user_id == current_user.id, APIKey.exchange == payload.exchange
        )
    )
    key: Optional[APIKey] = result.scalar_one_or_none()

    if key is None:
        key = APIKey(
            user_id=current_user.id,
            exchange=payload.exchange,
            encrypted_api_key=encrypt_value(payload.api_key),
            encrypted_api_secret=encrypt_value(payload.api_secret),
            verified=False,
        )
        db.add(key)
    else:
        key.encrypted_api_key = encrypt_value(payload.api_key)
        key.encrypted_api_secret = encrypt_value(payload.api_secret)
        key.verified = False  # re-verify after any credential change
        key.updated_at = to_db_datetime(utc_now())

    await db.commit()
    await db.refresh(key)
    return _to_response(key)


@router.get("/keys", response_model=List[APIKeyResponse])
async def list_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the authenticated user's API keys (masked)."""
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == current_user.id)
        .order_by(APIKey.created_at.desc())
    )
    return [_to_response(k) for k in result.scalars().all()]


class KeyVerifyResponse(BaseModel):
    verified: bool
    result: str  # ok | invalid_key | no_trade_permission | error
    detail: str
    withdrawal_permission_warning: bool = False


@router.post("/keys/{key_id}/verify", response_model=KeyVerifyResponse)
async def verify_key(
    key_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check the stored key against Binance and mark it verified if usable."""
    import asyncio
    from api.services.binance_client import BinanceClient, BinanceClientError

    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == current_user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    client = BinanceClient(
        decrypt_value(key.encrypted_api_key), decrypt_value(key.encrypted_api_secret))
    try:
        info = await asyncio.to_thread(client.get_account_info)
    except BinanceClientError as exc:
        key.verified = False
        await db.commit()
        invalid = str(exc.code) in ("-2014", "-2015", "-1022")
        return KeyVerifyResponse(
            verified=False,
            result="invalid_key" if invalid else "error",
            detail=("Key rejected by Binance — wrong key/secret, or this server's IP "
                    "is not on the key's allowlist") if invalid else str(exc),
        )

    if not info["can_trade"] or "SPOT" not in info["permissions"]:
        key.verified = False
        await db.commit()
        return KeyVerifyResponse(
            verified=False, result="no_trade_permission",
            detail="Key authenticates but lacks spot trading permission")

    key.verified = True
    key.updated_at = to_db_datetime(utc_now())
    await db.commit()
    return KeyVerifyResponse(
        verified=True, result="ok",
        detail="Key verified: spot trading enabled",
        withdrawal_permission_warning=info["can_withdraw"],
    )


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove one of the authenticated user's API keys."""
    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == current_user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(key)
    await db.commit()
