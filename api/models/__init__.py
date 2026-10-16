from sqlalchemy import Column, String, Text, Integer, Float, Boolean, DateTime, ForeignKey, Enum, Index
from sqlalchemy import text as sa_text
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
    notification_preferences = Column(Text, nullable=True)  # JSON blob; Text since migration 030
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    # "Sign out everywhere": tokens issued before this are refused (migration 029).
    # Kept apart from password_changed_at, whose email and wallet hold are tied to a password change.
    sessions_revoked_at = Column(DateTime(timezone=True), nullable=True)
    # Accounts created without a password (migrated Guardian members, customers an admin onboards)
    # carry this until they set one through the reset link; nothing reads it before v0.9.2 (migration 030).
    password_reset_required = Column(Boolean, nullable=False, default=False, server_default=sa_text("false"))
    # 24h hold on new withdrawal destinations after a password reset/change (migration 022)
    wallet_hold_until = Column(DateTime(timezone=True), nullable=True)
    wallet_hold_reason = Column(String(40), nullable=True)   # password_reset | password_change
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
    exchange = Column(String(50), nullable=False)  # bitget (binance until C3)
    encrypted_api_key = Column(String(500), nullable=False)
    encrypted_api_secret = Column(String(500), nullable=False)
    encrypted_passphrase = Column(String(500), nullable=True)  # Bitget requires one
    verified = Column(Boolean, nullable=False, default=False)   # executor gate; mirrors verification_status
    # Outcome of the last verification (migration 015). The error text is
    # what the exchange said, never credentials.
    verification_status = Column(String(20), nullable=False, default="pending")  # pending | verified | failed
    verification_error = Column(String(500), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    withdrawal_enabled = Column(Boolean, nullable=True)   # None = the exchange does not report permissions
    permissions = Column(String(500), nullable=True)      # codes reported at verification, comma-separated
    # Which Bitget API family the key's account speaks (migration 028): classic (v2) | unified (v3) | unknown.
    # unknown is treated as classic by every caller; 40085 on a v2 call switches the key to unified.
    account_mode = Column(String(10), nullable=False, default="unknown", server_default="unknown")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    user = relationship("User", back_populates="api_keys")

    @property
    def permission_list(self) -> list:
        return [x for x in (self.permissions or "").split(",") if x]


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
    status = Column(String(20), nullable=False)    # sent | queued (outbox) | failed | skipped_pref | skipped_quiet
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
# Two-factor authentication tables (migration 029).
from .security import UserTotp, UserTotpRecoveryCode  # noqa: E402,F401
# Guardian read-only monitoring tables (migration 030).
from . import guardian  # noqa: E402,F401
