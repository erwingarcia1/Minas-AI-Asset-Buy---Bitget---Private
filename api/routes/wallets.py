"""Saved withdrawal wallets, validated against the declared exchange chain (BTC, ERC20, SOL, TRC20, ...)."""
import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import User, Wallet, utcnow
from api.routes.user import get_current_user
from api.utils.address_denylist import DENYLIST_REASON, is_denylisted
from api.utils.chains import family_for_chain, normalize_chain, validate_address_for_chain
from api.utils.network_validator import detect_chain, get_address_network

logger = logging.getLogger("autobuy.wallets")

router = APIRouter()


class WalletCreateRequest(BaseModel):
    address: str = Field(min_length=20, max_length=128)
    blockchain_network: str
    label: Optional[str] = Field(default=None, max_length=100)

    @field_validator("blockchain_network")
    @classmethod
    def _network(cls, v: str) -> str:
        v = normalize_chain(v) or ""
        if family_for_chain(v) is None:
            raise ValueError("blockchain_network must be a supported exchange chain (e.g. BTC, ERC20, SOL, TRC20)")
        return v

    @field_validator("address")
    @classmethod
    def _address(cls, v: str) -> str:
        return v.strip()


class WalletOut(BaseModel):
    id: UUID
    address: str
    blockchain_network: str
    chain: Optional[str] = None
    label: Optional[str] = None
    verified_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


def _out(w: Wallet) -> WalletOut:
    return WalletOut(id=w.id, address=w.address, blockchain_network=w.blockchain_network,
                     chain=detect_chain(w.address), label=w.label, verified_at=w.verified_at, created_at=w.created_at)


@router.post("", response_model=WalletOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=WalletOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def add_wallet(payload: WalletCreateRequest, current_user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    """Save a wallet. The address must match the declared chain's format
    (Bitcoin mainnet, EVM 0x…, Solana, Tron); addresses are unique platform-wide."""
    from api.services import wallet_hold
    await wallet_hold.check_new_address(db, current_user, payload.address, "add_wallet")
    if is_denylisted(payload.address):
        raise HTTPException(status_code=422, detail={"error": "wallet_address_denylisted",
                                                    "details": {"address": payload.address, "message": DENYLIST_REASON}})
    ok, reason = validate_address_for_chain(payload.address, payload.blockchain_network)
    if not ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_wallet_address",
            "details": {"address": payload.address, "blockchain_network": payload.blockchain_network,
                        "detected_chain": detect_chain(payload.address),
                        "detected_network": get_address_network(payload.address), "message": reason}})
    existing = (await db.execute(select(Wallet).where(Wallet.address == payload.address))).scalar_one_or_none()
    if existing is not None:
        if existing.user_id == current_user.id:
            raise HTTPException(status_code=409, detail="Wallet already saved")
        raise HTTPException(status_code=409, detail="Address is already registered")
    wallet = Wallet(user_id=current_user.id, address=payload.address, blockchain_network=payload.blockchain_network,
                    label=payload.label, verified_at=utcnow())  # format-verified at save time
    db.add(wallet)
    await db.commit()
    await db.refresh(wallet)
    return _out(wallet)


@router.get("", response_model=List[WalletOut])
@router.get("/", response_model=List[WalletOut], include_in_schema=False)
async def list_wallets(blockchain_network: Optional[str] = None, current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """The caller's wallets, newest first; filter with `?blockchain_network=BTC|ERC20|...`."""
    q = select(Wallet).where(Wallet.user_id == current_user.id)
    if blockchain_network:
        q = q.where(Wallet.blockchain_network.ilike(blockchain_network))
    rows = (await db.execute(q.order_by(Wallet.created_at.desc()))).scalars().all()
    return [_out(w) for w in rows]


@router.delete("/{wallet_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_wallet(wallet_id: UUID, current_user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """Remove a saved wallet. Existing purchases keep their address."""
    w = (await db.execute(select(Wallet).where(Wallet.id == wallet_id, Wallet.user_id == current_user.id))).scalar_one_or_none()
    if w is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    await db.delete(w)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
