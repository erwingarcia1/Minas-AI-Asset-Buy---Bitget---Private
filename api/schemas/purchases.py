"""Schemas for /api/v2/purchases (moved out of api/routes/autobuy_purchases.py on 2026-09-22).

Class names are OpenAPI schema names: keep them stable."""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from api.models.purchase import PURCHASE_FREQUENCIES, PURCHASE_TYPES
from api.utils.address_denylist import DENYLIST_REASON, is_denylisted
from api.utils.chains import DEFAULT_CHAIN_FOR_COIN, family_for_chain, normalize_chain
from api.utils.exchange_networks import SUPPORTED_CRYPTOS

# The platform invents no minimum: Bitget's live per-pair minimum order (1 USDT today) is
# enforced by check_symbol at creation and again by the executor, and withdraw-immediately
# purchases must also clear the live per-chain withdrawal minimum + fee (check_withdrawal).
# 0.01 is only a sanity floor against zero/negative bodies. Both checks fail closed (503,
# purchase not created) when Bitget cannot be reached.
MIN_AMOUNT_USD, MAX_AMOUNT_USD = 0.01, 1_000_000.0


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
    purchase_type: str = "dca"   # accumulate by default (private beta); "autobuy" = withdraw immediately, gated
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
    exchange_key_error: Optional[str] = None   # the key's last verification problem (for example a Unified account); None while verified
    status: str
    failure_count: int
    last_error: Optional[str] = None
    next_execution: Optional[datetime] = None
    low_balance_for_next_run: bool = False          # a low-balance warning is outstanding for next_execution
    low_balance_alerted_at: Optional[datetime] = None
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


