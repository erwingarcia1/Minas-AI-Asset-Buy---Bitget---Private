"""Exchange API key management with Fernet encryption at rest."""
import base64
import hashlib
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, status, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.models import APIKey, User
from api.routes.user import UTCModel, get_current_user, to_db_datetime, utc_now

import logging
logger = logging.getLogger("autobuy.exchange")
from fastapi import Request
from api.services.rate_limit import limiter, user_or_ip_key

router = APIRouter()

SUPPORTED_EXCHANGES = {"binance", "bitget"}   # Bybit/OKX removed 2026-09-22 (audit C1); Binance goes with C3


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
    passphrase: str = Field(default="", max_length=256)  # Bitget requires one

    @field_validator("exchange")
    @classmethod
    def _supported(cls, value):
        value = value.lower()
        enabled = settings.supported_exchanges
        if value not in SUPPORTED_EXCHANGES or value not in enabled:
            raise ValueError(
                f"exchange must be one of: {', '.join(enabled)}"
            )
        return value


class APIKeyResponse(UTCModel):
    id: UUID
    exchange: str
    api_key_masked: str
    verified: bool
    verification_status: str = "pending"          # pending | verified | failed
    verification_error: Optional[str] = None      # readable reason when failed (never credentials)
    verified_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    withdrawal_enabled: Optional[bool] = None     # None = not reported by the exchange
    permissions: List[str] = []                   # codes the exchange reported at verification
    extra_permissions: List[str] = []             # recognised codes AutoBuy does not need (warn, non-blocking)
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _extra_permissions(key: APIKey) -> list:
    from api.services.exchange_client import BITGET_EXTRA_TOKENS
    if key.exchange != "bitget":
        return []
    return sorted(p for p in key.permission_list if p in BITGET_EXTRA_TOKENS)


def _to_response(key: APIKey) -> APIKeyResponse:
    return APIKeyResponse(
        id=key.id,
        exchange=key.exchange,
        api_key_masked=mask_api_key(decrypt_value(key.encrypted_api_key)),
        verified=bool(key.verified),
        verification_status=key.verification_status or "pending",
        verification_error=key.verification_error,
        verified_at=key.verified_at,
        last_checked_at=key.last_checked_at,
        withdrawal_enabled=key.withdrawal_enabled,
        permissions=key.permission_list,
        extra_permissions=_extra_permissions(key),
        created_at=key.created_at,
        updated_at=key.updated_at,
    )


@router.get("/supported", response_model=List[str])
async def supported_exchanges():
    """Exchanges this deployment offers (SUPPORTED_EXCHANGES); keys for others are refused."""
    return settings.supported_exchanges


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
            encrypted_passphrase=(encrypt_value(payload.passphrase)
                                  if payload.passphrase else None),
            verified=False,
        )
        db.add(key)
    else:
        key.encrypted_api_key = encrypt_value(payload.api_key)
        key.encrypted_api_secret = encrypt_value(payload.api_secret)
        key.encrypted_passphrase = (encrypt_value(payload.passphrase)
                                    if payload.passphrase else None)
        # re-verify after any credential change
        key.verified = False
        key.verification_status = "pending"
        key.verification_error = None
        key.verified_at = None
        key.withdrawal_enabled = None
        key.permissions = None
        key.updated_at = to_db_datetime(utc_now())

    await db.commit()
    await db.refresh(key)
    logger.info("api_key_upserted", extra={"user_id": str(current_user.id),
                                           "exchange": payload.exchange})
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
    result: str  # ok | invalid_key | ip_not_allowed | no_trade_permission | unreachable | error
    detail: str
    withdrawal_permission_warning: bool = False
    verification_status: str = "pending"
    verification_error: Optional[str] = None
    verified_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    withdrawal_enabled: Optional[bool] = None
    extra_permissions: List[str] = []             # non-blocking: key carries permissions AutoBuy does not need
    key: Optional[APIKeyResponse] = None


@router.post("/keys/{key_id}/verify", response_model=KeyVerifyResponse)
@limiter.limit("3/minute", key_func=user_or_ip_key)
async def verify_key(
    request: Request, response: Response,
    key_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check the stored key against its exchange with one harmless authenticated
    call (account info on Binance/Bitget, a balance read elsewhere) and record
    the outcome on the key: verified | failed with a readable reason,
    verified_at / last_checked_at, and whether the key can withdraw. Always
    200; ``verified`` and ``result`` carry the outcome."""
    from api.services.key_verification import verify_api_key

    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == current_user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if not key.verified:
        # private beta: a verified key lets this key's active withdraw-mode purchases run again
        from api.services import withdraw_gate
        await withdraw_gate.require_no_active_withdraw_mode(db, current_user, key_id=key.id)

    outcome = await verify_api_key(db, key)
    await db.refresh(key)
    return KeyVerifyResponse(
        verified=outcome.verified, result=outcome.result, detail=outcome.detail,
        # warn unless the venue confirmed the key cannot withdraw (None = unconfirmed/possible)
        withdrawal_permission_warning=outcome.verified and outcome.withdrawal_enabled is not False,
        verification_status=key.verification_status, verification_error=key.verification_error,
        verified_at=key.verified_at, last_checked_at=key.last_checked_at,
        withdrawal_enabled=key.withdrawal_enabled, extra_permissions=outcome.extra_permissions, key=_to_response(key),
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
    # Purchases on this key can never run again (the FK is set to NULL): stop
    # them now with a clear reason instead of letting the executor discover it.
    from api.models import Purchase
    from api.tasks.purchase_scheduler import KEY_REMOVED_MESSAGE
    affected = (await db.execute(select(Purchase).where(
        Purchase.exchange_account_id == key.id, Purchase.status.in_(("active", "paused"))))).scalars().all()
    for p in affected:
        p.status = "paused"
        p.next_execution = None
        p.last_error = KEY_REMOVED_MESSAGE
    await db.delete(key)
    await db.commit()
    logger.info("api_key_deleted", extra={"user_id": str(current_user.id), "key_id": str(key_id),
                                          "purchases_stopped": len(affected)})
