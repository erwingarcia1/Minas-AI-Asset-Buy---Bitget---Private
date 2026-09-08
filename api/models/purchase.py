"""Auto-Buy purchases with on-chain delivery: wallets, purchases, per-attempt transactions.

Distinct from the exchange-custody DCA schedules in ``RecurringPurchase`` /
``Transaction``: an Auto-Buy purchase buys on the chosen exchange account and
withdraws the coins to a validated wallet address on a declared network.
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from api.database import Base
from . import utcnow

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
    # Withdraw immediately (Auto-Buy) needs a wallet; accumulate-on-exchange (DCA) may leave it empty.
    wallet_address = Column(String(128), nullable=True)
    blockchain_network = Column(String(20), nullable=False)  # mainnet | testnet
    withdraw_immediately = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="active")
    failure_count = Column(Integer, nullable=False, default=0)  # consecutive failed attempts
    last_error = Column(String(500), nullable=True)
    next_execution = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("User", back_populates="purchases")
    exchange_account = relationship("APIKey")
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
    status = Column(String(20), nullable=False, default="pending")  # pending | success | failed
    amount_executed = Column(Float, nullable=True)   # crypto amount delivered
    fee = Column(Float, nullable=True)               # withdrawal fee in crypto units
    error_message = Column(String(500), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    purchase = relationship("Purchase", back_populates="transactions")
