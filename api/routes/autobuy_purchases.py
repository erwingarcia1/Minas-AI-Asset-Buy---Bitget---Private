"""Auto-Buy purchases: buy on an exchange account and deliver to a validated
wallet on a declared blockchain network. Mounted at /api/v2/purchases.

Every request validates (1) the wallet address format against the coin's
chain and the declared network, (2) that the exchange account can withdraw
to that network, and (3) that the caller owns the exchange account.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.database import get_db
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow
from api.models.purchase import PURCHASE_FREQUENCIES, PURCHASE_TYPES
from api.routes.user import get_current_user
from api.services import bitget_market, wallet_hold
from api.utils.address_denylist import DENYLIST_REASON, is_denylisted
from api.utils.chains import DEFAULT_CHAIN_FOR_COIN, check_destination, family_for_chain, normalize_chain, validate_address_for_chain
from api.utils.exchange_networks import SUPPORTED_CRYPTOS

logger = logging.getLogger("autobuy.purchases.v2")

router = APIRouter()

MIN_AMOUNT_USD, MAX_AMOUNT_USD = 10.0, 1_000_000.0


# --------------------------------------------------------------------------- schemas

class TravelRuleInfo(BaseModel):
    """Optional beneficiary / travel-rule details some venues require before an
    API withdrawal (Bitget: firstName, lastName, identityType, companyName,
    areaCode, memberCode). Stored on the purchase and sent with withdrawals.
    Nothing is prefilled: the KYC provider's data is not stored on the user."""
    first_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    identity_type: Optional[str] = Field(default=None, max_length=20)     # user | company (Bitget's values)
    company_name: Optional[str] = Field(default=None, max_length=200)
    area_code: Optional[str] = Field(default=None, max_length=20)
    member_code: Optional[str] = Field(default=None, max_length=100)

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    def to_json(self) -> Optional[str]:
        data = {k: v for k, v in self.model_dump().items() if v}
        return json.dumps(data) if data else None


class PurchaseCreateRequest(BaseModel):
    crypto_symbol: str = Field(min_length=2, max_length=10)
    amount_usd: float = Field(ge=MIN_AMOUNT_USD, le=MAX_AMOUNT_USD)
    # autobuy: required. dca (accumulate): optional — nothing is ever withdrawn.
    wallet_address: Optional[str] = Field(default=None, max_length=128)
    # The exchange's chain name for the withdrawal (BTC, ERC20, SOL, TRC20, ...). Defaults to the coin's native chain.
    blockchain_network: Optional[str] = None
    frequency: str
    exchange_account_id: UUID
    # autobuy (default): buy, then withdraw to wallet_address right away
    # dca:               buy and accumulate on the exchange; the wallet is kept for later withdrawals
    purchase_type: str = "autobuy"
    # Deprecated alias for purchase_type (true = autobuy, false = dca); must agree with it when both are sent.
    withdraw_immediately: Optional[bool] = None
    travel_rule_info: Optional[TravelRuleInfo] = None
    # Tag chains (XRP Ledger, Stellar): who the address belongs to and the tag/memo.
    destination_type: Optional[str] = Field(default=None, max_length=20)   # self_custody | exchange
    memo: Optional[str] = Field(default=None, max_length=64)

    @field_validator("purchase_type")
    @classmethod
    def _purchase_type(cls, v: str) -> str:
        v = (v or "autobuy").lower().strip().replace("-", "").replace("_", "")
        if v not in PURCHASE_TYPES:
            raise ValueError(f"purchase_type must be one of {', '.join(PURCHASE_TYPES)}")
        return v

    @model_validator(mode="after")
    def _reconcile_alias(self):
        if self.withdraw_immediately is not None:
            implied = "autobuy" if self.withdraw_immediately else "dca"
            if "purchase_type" in self.model_fields_set and self.purchase_type != implied:
                raise ValueError("withdraw_immediately contradicts purchase_type; send purchase_type only")
            self.purchase_type = implied
        if self.purchase_type == "autobuy" and not self.wallet_address:
            raise ValueError("wallet_address is required for withdraw-immediately (autobuy) purchases")
        if self.wallet_address is not None and len(self.wallet_address) < 20:
            raise ValueError("wallet_address is too short to be a valid address")
        # A chain matters only when coins leave the exchange: autobuy, or a wallet kept on file.
        # Accumulate purchases without a wallet store no chain ("Held on the exchange").
        if self.blockchain_network is None and (self.purchase_type == "autobuy" or self.wallet_address):
            self.blockchain_network = DEFAULT_CHAIN_FOR_COIN.get(self.crypto_symbol, "ERC20")
        return self

    @field_validator("crypto_symbol")
    @classmethod
    def _crypto(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in SUPPORTED_CRYPTOS:
            raise ValueError(f"unsupported crypto; choose one of {', '.join(SUPPORTED_CRYPTOS)}")
        return v

    @field_validator("blockchain_network")
    @classmethod
    def _network(cls, v):
        v = normalize_chain(v)
        if v is None:
            return None
        if family_for_chain(v) is None:
            raise ValueError("blockchain_network must be a supported exchange chain (e.g. BTC, ERC20, SOL, TRC20)")
        return v

    @field_validator("frequency")
    @classmethod
    def _frequency(cls, v: str) -> str:
        v = v.lower().strip().replace("-", "_").replace(" ", "_")
        if v == "once":
            v = "one_time"
        if v not in PURCHASE_FREQUENCIES:
            raise ValueError(f"frequency must be one of {', '.join(PURCHASE_FREQUENCIES)}")
        return v

    @field_validator("wallet_address")
    @classmethod
    def _address(cls, v):
        v = (v or "").strip()
        return v or None


class PurchaseUpdateRequest(BaseModel):
    status: Optional[str] = None            # active | paused
    amount_usd: Optional[float] = Field(default=None, ge=MIN_AMOUNT_USD, le=MAX_AMOUNT_USD)
    frequency: Optional[str] = None
    # Mode switch. Going to autobuy (withdraw immediately) needs a real wallet
    # address, its network, and the address typed again as confirmation.
    purchase_type: Optional[str] = None
    wallet_address: Optional[str] = Field(default=None, max_length=128)
    blockchain_network: Optional[str] = None
    confirm_wallet_address: Optional[str] = None
    travel_rule_info: Optional[TravelRuleInfo] = None   # replaces the stored details; send {} to clear
    destination_type: Optional[str] = Field(default=None, max_length=20)
    memo: Optional[str] = Field(default=None, max_length=64)

    @field_validator("purchase_type")
    @classmethod
    def _purchase_type(cls, v):
        return None if v is None else PurchaseCreateRequest._purchase_type(v)

    @field_validator("blockchain_network")
    @classmethod
    def _network(cls, v):
        return PurchaseCreateRequest._network(v)

    @field_validator("wallet_address", "confirm_wallet_address")
    @classmethod
    def _address(cls, v):
        v = (v or "").strip()
        return v or None

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is None:
            return v
        v = v.lower().strip()
        if v not in ("active", "paused"):
            raise ValueError("status must be 'active' (resume) or 'paused' (pause)")
        return v

    @field_validator("frequency")
    @classmethod
    def _frequency(cls, v):
        return None if v is None else PurchaseCreateRequest._frequency(v)


class TransactionOut(BaseModel):
    id: UUID
    purchase_id: UUID
    exchange_tx_id: Optional[str] = None
    tx_type: str = "withdrawal"   # buy | withdrawal | accumulation
    status: str                   # pending | success | failed | needs_review | needs_info | simulated (dry-run withdrawal)
    amount_executed: Optional[float] = None   # withdrawal rows: the amount handed to the exchange
    fee: Optional[float] = None
    error_message: Optional[str] = None
    executed_at: Optional[datetime] = None
    created_at: datetime
    # buy rows
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    cost_usd: Optional[float] = None
    fee_currency: Optional[str] = None
    # withdrawal rows (parent_transaction_id -> the buy whose coins were sent)
    parent_transaction_id: Optional[UUID] = None
    withdrawal_id: Optional[str] = None
    withdrawal_chain: Optional[str] = None
    withdrawal_fee: Optional[float] = None
    withdrawal_fee_currency: Optional[str] = None
    expected_received: Optional[float] = None   # withdrawal rows: what the wallet should receive
    withdrawal_memo: Optional[str] = None       # withdrawal rows: the tag/memo sent
    tx_hash: Optional[str] = None
    raw_status: Optional[str] = None

    model_config = {"from_attributes": True}


class PurchaseOut(BaseModel):
    id: UUID
    crypto_symbol: str
    amount_usd: float
    frequency: str
    wallet_address: Optional[str] = None   # None on accumulate purchases without a wallet on file
    blockchain_network: Optional[str] = None   # None on accumulate purchases: coins are held on the exchange
    wallet_destination_type: Optional[str] = None   # self_custody | exchange (tag chains only)
    wallet_memo: Optional[str] = None               # destination tag / memo sent with withdrawals
    purchase_type: str            # autobuy | dca
    withdraw_immediately: bool    # derived: purchase_type == "autobuy" (compatibility)
    exchange_account_id: Optional[UUID] = None
    exchange: Optional[str] = None
    exchange_key_removed: bool = False   # the key was deleted: the purchase cannot run again
    status: str
    failure_count: int
    last_error: Optional[str] = None
    next_execution: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    latest_transaction: Optional[TransactionOut] = None
    travel_rule_info: Optional[TravelRuleInfo] = None

    model_config = {"from_attributes": True}


class PurchaseDetailOut(PurchaseOut):
    transactions: List[TransactionOut] = []


class PurchaseCreatedOut(BaseModel):
    purchase_id: UUID
    status: str
    next_execution: Optional[datetime] = None


class RetryWithdrawalOut(BaseModel):
    purchase_id: UUID
    status: str                       # the purchase's status after the retry
    next_execution: Optional[datetime] = None
    withdrawal: TransactionOut        # the new withdrawal row (success | failed | needs_info)


# --------------------------------------------------------------------------- helpers

def _tx_out(t: PurchaseTransaction) -> TransactionOut:
    """API view of a row (executor vocabulary): a dry-run withdrawal shows "simulated"."""
    from api.services.history import is_simulated
    out = TransactionOut.model_validate(t)
    if is_simulated(t):
        out.status = "simulated"
    return out


def _to_out(p: Purchase, detail: bool = False) -> PurchaseOut:
    txs = list(p.transactions or [])
    txs.sort(key=lambda t: t.created_at, reverse=True)
    base = dict(
        id=p.id, crypto_symbol=p.crypto_symbol, amount_usd=p.amount_usd, frequency=p.frequency,
        wallet_address=p.wallet_address, blockchain_network=p.blockchain_network,
        wallet_destination_type=p.wallet_destination_type, wallet_memo=p.wallet_memo,
        purchase_type=p.purchase_type, withdraw_immediately=p.purchase_type == "autobuy",
        exchange_account_id=p.exchange_account_id,
        exchange=p.exchange_account.exchange if p.exchange_account else None,
        exchange_key_removed=p.exchange_account is None,
        status=p.status, failure_count=p.failure_count, last_error=p.last_error,
        next_execution=p.next_execution, created_at=p.created_at, updated_at=p.updated_at,
        latest_transaction=_tx_out(txs[0]) if txs else None,
        travel_rule_info=TravelRuleInfo(**p.travel_rule_dict()) if p.travel_rule_dict() else None,
    )
    if detail:
        return PurchaseDetailOut(**base, transactions=[_tx_out(t) for t in txs])
    return PurchaseOut(**base)


async def _owned_purchase(db: AsyncSession, purchase_id: UUID, user: User) -> Purchase:
    p = (await db.execute(
        select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.exchange_account),
                                 selectinload(Purchase.user))
        .where(Purchase.id == purchase_id, Purchase.user_id == user.id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
    return p


def _reject(error: str, details: dict) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": error, "details": details})


def _check_wallet(address: str, chain: str, crypto: str, destination_type: Optional[str] = None,
                  memo: Optional[str] = None) -> dict:
    """422 unless the address is a valid, spendable address for the chain's format
    and, on tag chains, the destination question is answered consistently.
    Returns the normalised destination {address, destination_type, memo}."""
    if is_denylisted(address):
        raise _reject("wallet_address_denylisted", {
            "wallet_address": address, "message": DENYLIST_REASON})
    d = check_destination(address, chain, destination_type, memo)
    if not d["ok"]:
        raise _reject(d["error"], {
            "wallet_address": address, "blockchain_network": chain, "expected_family": family_for_chain(chain),
            "needs_tag": d.get("needs_tag"), "message": f"{d['message']} ({crypto} on {chain})",
        })
    return d


async def _validate_against_exchange(account: APIKey, crypto: str, amount_usd: float, chain: Optional[str],
                                     withdraw: bool) -> Optional[dict]:
    """Bitget accounts: the pair must exist and be online, the order must clear
    Bitget's minimum, and for withdraw-immediately the chain must be one Bitget
    withdraws on and the amount after fees must clear the minimum withdrawal
    (priced at the live best ask). Returns the estimate for autobuy."""
    if account.exchange != "bitget":
        return None
    try:
        symbol = await bitget_market.get_symbol(crypto)
        bitget_market.check_symbol(crypto, amount_usd, symbol)
        if chain is not None:
            chains = await bitget_market.get_chains(crypto)
            entry = bitget_market.find_chain(chains, chain)
            if entry is None:
                raise bitget_market.PurchaseValidationError("chain_not_available", {
                    "coin": crypto, "chain": chain, "available_chains": [c["chain"] for c in chains],
                    "message": f"Bitget does not list {chain} as a chain for {crypto}"})
            if withdraw:
                price = await asyncio.to_thread(bitget_market.live_ask, crypto)
                return bitget_market.check_withdrawal(crypto, amount_usd, price, symbol, entry)
    except bitget_market.PurchaseValidationError as exc:
        raise _reject(exc.error, exc.details)
    except bitget_market.MarketDataUnavailable as exc:
        logger.warning("purchase validation: market data unavailable for %s: %s", crypto, exc)
        raise HTTPException(status_code=503, detail={"error": "exchange_data_unavailable",
                                                    "details": {"message": bitget_market.UNAVAILABLE_MESSAGE}})
    return None


# --------------------------------------------------------------------------- routes

@router.post("", response_model=PurchaseCreatedOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=PurchaseCreatedOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_purchase(
    payload: PurchaseCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a purchase. `purchase_type` selects the mode: `autobuy` (default:
    buy, then withdraw the coins to `wallet_address`, which is required) or
    `dca` (buy and accumulate on the exchange; no wallet needed — one may be
    given for a later switch). Rejects (422 with `{error, details}`) when a
    wallet address does not match the chain's format, is a known burn address,
    when Bitget has no online USDT pair for the coin, when the order is below
    Bitget's minimum, when the chain is not withdrawable, when the amount after
    fees is below Bitget's minimum withdrawal (autobuy), or when the exchange
    account is not the caller's."""
    destination = None
    if payload.wallet_address is not None:
        destination = _check_wallet(payload.wallet_address, payload.blockchain_network, payload.crypto_symbol,
                                    payload.destination_type, payload.memo)
        # after a password reset/change: only addresses this user already uses, for 24h
        await wallet_hold.check_new_address(db, current_user, destination["address"], "create_purchase")
    elif payload.destination_type or payload.memo:
        raise _reject("wallet_address_required", {"message": "destination_type and memo describe a wallet address; add the address"})

    account = (await db.execute(select(APIKey).where(
        APIKey.id == payload.exchange_account_id, APIKey.user_id == current_user.id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail={"error": "exchange_account_not_found",
                                                    "details": {"exchange_account_id": str(payload.exchange_account_id)}})

    await _validate_against_exchange(account, payload.crypto_symbol, payload.amount_usd,
                                     payload.blockchain_network if payload.wallet_address else None,
                                     withdraw=payload.purchase_type == "autobuy")

    purchase = Purchase(
        user_id=current_user.id, exchange_account_id=account.id, crypto_symbol=payload.crypto_symbol,
        amount_usd=payload.amount_usd, frequency=payload.frequency,
        wallet_address=destination["address"] if destination else None,
        wallet_destination_type=destination["destination_type"] if destination else None,
        wallet_memo=destination["memo"] if destination else None,
        blockchain_network=payload.blockchain_network, purchase_type=payload.purchase_type,
        travel_rule_info=payload.travel_rule_info.to_json() if payload.travel_rule_info else None,
        status="active", failure_count=0,
        next_execution=utcnow(),  # due on the scheduler's next run
    )
    db.add(purchase)
    await db.commit()
    await db.refresh(purchase)
    logger.info("purchase created", extra={"purchase_id": str(purchase.id), "user_id": str(current_user.id),
                                           "crypto": purchase.crypto_symbol, "network": purchase.blockchain_network,
                                           "purchase_type": purchase.purchase_type})
    return PurchaseCreatedOut(purchase_id=purchase.id, status=purchase.status, next_execution=purchase.next_execution)


@router.get("", response_model=List[PurchaseOut])
@router.get("/", response_model=List[PurchaseOut], include_in_schema=False)
async def list_purchases(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """All of the caller's Auto-Buy purchases, each with its latest transaction."""
    rows = (await db.execute(
        select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.exchange_account))
        .where(Purchase.user_id == current_user.id).order_by(Purchase.created_at.desc()))).scalars().all()
    return [_to_out(p) for p in rows]


@router.get("/{purchase_id}", response_model=PurchaseDetailOut)
async def get_purchase(purchase_id: UUID, current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """One purchase with its full transaction history (newest first)."""
    return _to_out(await _owned_purchase(db, purchase_id, current_user), detail=True)


@router.put("/{purchase_id}", response_model=PurchaseOut)
async def update_purchase(purchase_id: UUID, payload: PurchaseUpdateRequest,
                          current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Pause (`status: paused`), resume (`status: active`), change amount/frequency,
    or switch mode. Switching to `autobuy` (withdraw immediately) requires
    `wallet_address`, `blockchain_network` and `confirm_wallet_address` equal to
    the address; the address must be valid, spendable and supported by the
    exchange. Cancelled, completed and failed purchases cannot be changed."""
    p = await _owned_purchase(db, purchase_id, current_user)
    if p.status in ("cancelled", "completed", "failed"):
        raise HTTPException(status_code=409, detail=f"Purchase is {p.status} and cannot be changed")
    if (payload.status is None and payload.amount_usd is None and payload.frequency is None
            and payload.purchase_type is None and payload.wallet_address is None and payload.travel_rule_info is None
            and payload.destination_type is None and payload.memo is None):
        raise HTTPException(status_code=400, detail="Nothing to update")
    if payload.travel_rule_info is not None:
        p.travel_rule_info = payload.travel_rule_info.to_json()
    if payload.wallet_address is None and (payload.destination_type is not None or payload.memo is not None):
        if not p.wallet_address:
            raise _reject("wallet_address_required", {"message": "destination_type and memo describe a wallet address; add the address first"})
        d = _check_wallet(p.wallet_address, p.blockchain_network, p.crypto_symbol,
                          payload.destination_type if payload.destination_type is not None else p.wallet_destination_type,
                          payload.memo if payload.memo is not None else p.wallet_memo)
        p.wallet_destination_type, p.wallet_memo = d["destination_type"], d["memo"]
    if payload.purchase_type == "autobuy" and p.purchase_type != "autobuy":
        # switching ON withdraw-immediately: blocked outright during the wallet-change hold,
        # otherwise real address + network + typed confirmation, every time
        wallet_hold.check_action(current_user, "switch_to_autobuy")
        address = payload.wallet_address
        network = payload.blockchain_network or p.blockchain_network or DEFAULT_CHAIN_FOR_COIN.get(p.crypto_symbol)
        if not address:
            raise _reject("wallet_address_required", {
                "message": "Switching to withdraw immediately needs the wallet address the coins will be sent to"})
        if payload.confirm_wallet_address != address:
            raise _reject("wallet_confirmation_mismatch", {
                "message": "Type the wallet address again in confirm_wallet_address to confirm the switch"})
        d = _check_wallet(address, network, p.crypto_symbol,
                          payload.destination_type if payload.destination_type is not None else p.wallet_destination_type,
                          payload.memo if payload.memo is not None else p.wallet_memo)
        if p.exchange_account is not None:
            await _validate_against_exchange(p.exchange_account, p.crypto_symbol, float(payload.amount_usd or p.amount_usd),
                                             network, withdraw=True)
        p.wallet_address, p.blockchain_network, p.purchase_type = d["address"], network, "autobuy"
        p.wallet_destination_type, p.wallet_memo = d["destination_type"], d["memo"]
        logger.info("purchase switched to autobuy", extra={"purchase_id": str(p.id), "user_id": str(current_user.id),
                                                           "network": network})
    elif payload.purchase_type == "dca" and p.purchase_type != "dca":
        p.purchase_type = "dca"   # the wallet on file is kept for a later switch back
        logger.info("purchase switched to dca", extra={"purchase_id": str(p.id), "user_id": str(current_user.id)})
    elif payload.wallet_address is not None:
        # changing the wallet on an existing purchase: same checks as creation
        network = payload.blockchain_network or p.blockchain_network or DEFAULT_CHAIN_FOR_COIN.get(p.crypto_symbol)
        d = _check_wallet(payload.wallet_address, network, p.crypto_symbol, payload.destination_type, payload.memo)
        await wallet_hold.check_new_address(db, current_user, d["address"], "change_wallet")
        if p.purchase_type == "autobuy" and payload.confirm_wallet_address != payload.wallet_address:
            raise _reject("wallet_confirmation_mismatch", {
                "message": "Type the wallet address again in confirm_wallet_address to change where coins are sent"})
        p.wallet_address, p.blockchain_network = d["address"], network
        p.wallet_destination_type, p.wallet_memo = d["destination_type"], d["memo"]
    if payload.status == "active" and p.status != "active" and p.exchange_account is None:
        raise HTTPException(status_code=409, detail={
            "error": "exchange_key_removed",
            "details": {"message": "Exchange key removed. This purchase cannot run again; add an exchange key and create a new purchase."}})
    if payload.status == "active" and p.status == "paused":
        p.failure_count = 0
        p.last_error = None
        if p.next_execution is None or p.next_execution < utcnow():
            p.next_execution = utcnow()
    if payload.status is not None:
        p.status = payload.status
    if payload.amount_usd is not None:
        p.amount_usd = payload.amount_usd
    if payload.frequency is not None:
        p.frequency = payload.frequency
    await db.commit()
    await db.refresh(p)
    return _to_out(p)


def _gateway():
    """The executor gateway used for manual withdrawal retries (patched in tests)."""
    from api.tasks.purchase_scheduler import ExchangeClientGateway
    return ExchangeClientGateway()


@router.post("/{purchase_id}/retry-withdrawal", response_model=RetryWithdrawalOut)
async def retry_withdrawal(purchase_id: UUID, current_user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """Send the coins of the last filled buy to the wallet again. Only for
    purchases parked as `withdrawal_failed` or `needs_info`; the buy is never
    repeated. The wallet, chain and Bitget minimum are re-checked first. On
    success the purchase becomes `active` again and the schedule resumes; a
    failed retry leaves it parked with the new error. Runs in dry-run mode
    when AUTOBUY_DRY_RUN is set (nothing is sent)."""
    from api.tasks import purchase_scheduler as ps
    p = await _owned_purchase(db, purchase_id, current_user)
    if p.status not in ("withdrawal_failed", "needs_info"):
        raise HTTPException(status_code=409, detail={
            "error": "not_retryable",
            "details": {"status": p.status, "message": f"Purchase is {p.status}; only withdrawal_failed / needs_info purchases can retry a withdrawal"}})
    if not p.wallet_address:
        raise _reject("wallet_address_required", {"message": "The purchase has no wallet address to withdraw to"})
    if ps.stranded_buy(p) is None:
        raise HTTPException(status_code=409, detail={"error": "nothing_to_retry",
                                                    "details": {"message": "No filled buy with a failed withdrawal was found"}})
    if p.exchange_account is None or not p.exchange_account.verified:
        raise _reject("exchange_key_unverified", {"message": "The purchase's exchange key is missing or unverified"})
    _check_wallet(p.wallet_address, p.blockchain_network, p.crypto_symbol, p.wallet_destination_type, p.wallet_memo)
    await _validate_against_exchange(p.exchange_account, p.crypto_symbol, float(p.amount_usd), p.blockchain_network, withdraw=True)
    wd = await ps.retry_withdrawal(db, p, gateway=_gateway())
    logger.info("withdrawal retried", extra={"purchase_id": str(p.id), "user_id": str(current_user.id),
                                             "withdrawal_id": str(wd.id), "result": wd.status})
    return RetryWithdrawalOut(purchase_id=p.id, status=p.status, next_execution=p.next_execution,
                              withdrawal=_tx_out(wd))


@router.delete("/{purchase_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def cancel_purchase(purchase_id: UUID, current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """Cancel a purchase. The record and its transaction history are kept;
    the scheduler will never execute it again."""
    p = await _owned_purchase(db, purchase_id, current_user)
    p.status = "cancelled"
    p.next_execution = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{purchase_id}/transactions", response_model=List[TransactionOut])
async def purchase_transactions(purchase_id: UUID, status_filter: Optional[str] = None,
                                current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Transaction history for a purchase, optionally filtered by status
    (pending / success / failed)."""
    p = await _owned_purchase(db, purchase_id, current_user)
    txs = sorted(p.transactions, key=lambda t: t.created_at, reverse=True)
    if status_filter:
        txs = [t for t in txs if t.status == status_filter.lower()]
    return [_tx_out(t) for t in txs]


# re-export for tests
__all__ = ["router", "PurchaseCreateRequest", "timedelta", "PurchaseTransaction"]
