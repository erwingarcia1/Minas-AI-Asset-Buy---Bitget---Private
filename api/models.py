from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Enum, Index
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime, timezone
from api.database import Base


def utcnow():
    """Timezone-aware UTC timestamp for column defaults."""
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    kyc_status = Column(String(50), default="pending")  # pending, verified, rejected
    kyc_sumsub_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")
    recurring_purchases = relationship("RecurringPurchase", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    exchange = Column(String(50), nullable=False)  # binance, coinbase, etc.
    encrypted_api_key = Column(String(500), nullable=False)
    encrypted_api_secret = Column(String(500), nullable=False)
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    user = relationship("User", back_populates="api_keys")


class RecurringPurchase(Base):
    __tablename__ = "recurring_purchases"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    crypto = Column(String(10), nullable=False)  # BTC, ETH, etc.
    amount_usdt = Column(Float, nullable=False)  # 500.00
    frequency = Column(String(50), nullable=False)  # daily, weekly, biweekly, monthly
    next_purchase_date = Column(DateTime(timezone=True), nullable=False)
    withdrawal_address = Column(String(255), nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    user = relationship("User", back_populates="recurring_purchases")
    # Transactions are the audit trail: deleting a schedule must NOT delete them.
    # The FK is ON DELETE SET NULL and the DB handles it (passive_deletes).
    transactions = relationship("Transaction", back_populates="recurring_purchase", passive_deletes="all")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_user_id_created_at", "user_id", "created_at"),
    )
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    recurring_purchase_id = Column(
        UUID(as_uuid=True),
        ForeignKey("recurring_purchases.id", ondelete="SET NULL"),
        nullable=True,
    )
    crypto = Column(String(10), nullable=False)
    amount_crypto = Column(Float, nullable=False)
    cost_usdt = Column(Float, nullable=False)
    binance_order_id = Column(String(255), nullable=True)
    status = Column(String(50), default="pending")  # pending, completed, failed
    error_message = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    user = relationship("User", back_populates="transactions")
    recurring_purchase = relationship("RecurringPurchase", back_populates="transactions")
