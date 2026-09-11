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
    wallets = relationship("Wallet", back_populates="user", cascade="all, delete-orphan")
    purchases = relationship("Purchase", back_populates="user", cascade="all, delete-orphan")


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


# Auto-Buy purchases (wallets, purchases, purchase_transactions) live in their
# own module; importing them here registers the tables on Base.metadata.
from .purchase import Purchase, PurchaseTransaction, Wallet  # noqa: E402,F401
