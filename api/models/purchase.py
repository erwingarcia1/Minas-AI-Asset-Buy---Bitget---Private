"""Auto-Buy purchases with on-chain delivery: wallets, purchases, per-attempt transactions.

An Auto-Buy purchase buys on the chosen exchange account and either withdraws
the coins to a validated wallet on a declared network (``autobuy``) or keeps
them on the exchange (``dca``). ``purchase_transactions`` is the platform's
only execution history.
"""
import uuid

from sqlalchemy import Column, DateTime, Enum, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from api.database import Base
from . import utcnow

PURCHASE_TYPES = ("autobuy", "dca")           # autobuy: buy + withdraw to wallet; dca: buy + accumulate on exchange
TRANSACTION_TYPES = ("withdrawal", "accumulation")
PURCHASE_FREQUENCIES = ("one_time", "daily", "weekly", "monthly")
PURCHASE_STATUSES = ("active", "paused", "completed", "failed", "cancelled")
TRANSACTION_STATUSES = ("pending", "success", "failed")
BLOCKCHAIN_NETWORKS = ("mainnet", "testnet")


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    address = Column(String(128), nullable=False, unique=True)
    blockchain_network = Column(String(20), nullable=False)  # mainnet | testnet
    label = Column(String(100), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="wallets")


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        Index("ix_purchases_status_next_execution", "status", "next_execution"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    exchange_account_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    crypto_symbol = Column(String(10), nullable=False)
    amount_usd = Column(Float, nullable=False)
    frequency = Column(String(20), nullable=False)          # one_time | daily | weekly | monthly
    # autobuy: required, withdrawn to after every buy. dca: optional (kept for a later switch to withdraw).
    wallet_address = Column(String(128), nullable=True)
    blockchain_network = Column(String(20), nullable=False)  # mainnet | testnet
    purchase_type = Column(Enum(*PURCHASE_TYPES, name="purchase_type"), nullable=False, default="autobuy",
                           server_default="autobuy")
    status = Column(String(20), nullable=False, default="active")
    failure_count = Column(Integer, nullable=False, default=0)  # consecutive failed attempts
    last_error = Column(String(500), nullable=True)
    next_execution = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("User", back_populates="purchases")
    exchange_account = relationship("APIKey")

    @property
    def withdraw_immediately(self) -> bool:
        """Compatibility view of purchase_type for older clients."""
        return self.purchase_type == "autobuy"
    transactions = relationship("PurchaseTransaction", back_populates="purchase",
                                cascade="all, delete-orphan", order_by="PurchaseTransaction.created_at.desc()")


class PurchaseTransaction(Base):
    """One execution attempt of a Purchase (buy + withdrawal)."""
    __tablename__ = "purchase_transactions"
    __table_args__ = (
        Index("ix_purchase_transactions_purchase_created", "purchase_id", "created_at"),
        UniqueConstraint("exchange_tx_id", name="uq_purchase_transactions_exchange_tx_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id = Column(UUID(as_uuid=True), ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False)
    exchange_tx_id = Column(String(128), nullable=True)
    # withdrawal: coins were sent to the wallet (exchange_tx_id = withdrawal id)
    # accumulation: coins stay on the exchange (exchange_tx_id = order id)
    tx_type = Column(Enum(*TRANSACTION_TYPES, name="transaction_type"), nullable=False, default="withdrawal",
                     server_default="withdrawal")
    status = Column(String(20), nullable=False, default="pending")  # pending | success | failed
    amount_executed = Column(Float, nullable=True)   # crypto amount bought
    fee = Column(Float, nullable=True)               # trading fee (see fee_currency)
    error_message = Column(String(500), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Fill details (migration 013) — the single source of truth for history,
    # dashboards, tax exports and notifications.
    order_id = Column(String(128), nullable=True)            # exchange order id of the buy
    fill_price = Column(Float, nullable=True)                # USDT per coin at fill
    cost_usd = Column(Float, nullable=True)                  # quote actually spent
    fee_currency = Column(String(10), nullable=True)
    withdrawal_id = Column(String(128), nullable=True)       # autobuy: exchange withdrawal id
    withdrawal_fee = Column(Float, nullable=True)
    withdrawal_fee_currency = Column(String(10), nullable=True)
    tx_hash = Column(String(128), nullable=True)             # on-chain hash once the exchange reports it

    purchase = relationship("Purchase", back_populates="transactions")
