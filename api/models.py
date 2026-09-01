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
    role = Column(String(20), nullable=False, default="user")  # user | admin
    suspended = Column(Boolean, nullable=False, default=False)
    phone_number = Column(String(32), nullable=True)          # E.164
    phone_verified = Column(Boolean, nullable=False, default=False)
    device_token = Column(String(255), nullable=True)         # FCM registration token
    notification_preferences = Column(String(500), nullable=True)  # JSON blob
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")
    recurring_purchases = relationship("RecurringPurchase", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    exchange = Column(String(50), nullable=False)  # binance, bitget, bybit, okx
    encrypted_api_key = Column(String(500), nullable=False)
    encrypted_api_secret = Column(String(500), nullable=False)
    encrypted_passphrase = Column(String(500), nullable=True)  # OKX requires one
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    user = relationship("User", back_populates="api_keys")


class RecurringPurchase(Base):
    __tablename__ = "recurring_purchases"
    __table_args__ = (
        # the scheduler's due-purchases scan
        Index("ix_recurring_purchases_enabled_next", "enabled", "next_purchase_date"),
    )
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    crypto = Column(String(10), nullable=False)  # BTC, ETH, etc.
    exchange = Column(String(20), nullable=False, default="binance")
    amount_usdt = Column(Float, nullable=False)  # 500.00
    frequency = Column(String(50), nullable=False)  # daily, weekly, biweekly, monthly
    next_purchase_date = Column(DateTime(timezone=True), nullable=False)
    withdrawal_address = Column(String(255), nullable=True)
    enabled = Column(Boolean, default=True)
    # Advanced purchase behavior (issue #25):
    # recurring       -- plain DCA (default)
    # one_time        -- executes once, then disables itself
    # limit_order     -- one-shot, executes only once price <= limit_price
    # rebalancing     -- member of a target-allocation plan
    purchase_type = Column(String(20), nullable=False, default="recurring")
    limit_price = Column(Float, nullable=True)      # execute only at/below this price
    min_price = Column(Float, nullable=True)        # DCA band floor (skip below)
    max_price = Column(Float, nullable=True)        # DCA band ceiling (skip above)
    volatility_enabled = Column(Boolean, nullable=False, default=False)
    target_allocation = Column(Float, nullable=True)  # percent, for rebalancing plans
    expires_at = Column(DateTime(timezone=True), nullable=True)  # limit orders
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
        Index("ix_transactions_user_id_status", "user_id", "status"),
        Index("ix_transactions_executed_at", "executed_at"),
        Index("ix_transactions_binance_order_id", "binance_order_id"),
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
    exchange_rate = Column(Float, nullable=True)  # quote/base price at execution
    executed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(50), default="pending")  # pending, completed, failed
    error_message = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    user = relationship("User", back_populates="transactions")
    recurring_purchase = relationship("RecurringPurchase", back_populates="transactions")


class Notification(Base):
    """Audit log of every outbound notification attempt, all channels."""
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(10), nullable=False)   # email | sms | push
    template = Column(String(50), nullable=False)
    subject = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False)    # sent | failed | skipped_pref | skipped_quiet
    created_at = Column(DateTime(timezone=True), default=utcnow)


class WebhookEndpoint(Base):
    """User-configured outbound webhook (purchase events, HMAC-signed)."""
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    url = Column(String(500), nullable=False)
    secret = Column(String(128), nullable=False)   # HMAC key, shown once at creation
    enabled = Column(Boolean, nullable=False, default=True)
    last_status = Column(String(100), nullable=True)  # e.g. "200" or "failed: timeout"
    created_at = Column(DateTime(timezone=True), default=utcnow)
