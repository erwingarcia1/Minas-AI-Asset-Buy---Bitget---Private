"""Two-factor authentication tables (migration 029, v0.9.0).

One enrolment per user. The shared secret is a credential exactly as much as
an exchange API key is, and it is stored under the same cipher
(api/services/crypto.py): a database read must not yield a working
authenticator. `confirmed` is false while enrolling, so 2FA is not in force
until the customer has proved they can produce a code. `last_used_step` is
replay protection: each 30-second step is accepted once.

Recovery codes bypass the second factor, so they are stored hashed and each
is usable once (`used_at` set, row kept).
"""
import uuid

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID

from api.database import Base
from . import utcnow


class UserTotp(Base):
    __tablename__ = "user_totp"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    encrypted_secret = Column(String(500), nullable=False)
    confirmed = Column(Boolean, nullable=False, default=False, server_default="false")
    last_used_step = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)


class UserTotpRecoveryCode(Base):
    __tablename__ = "user_totp_recovery_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_user_totp_recovery_codes_user_used", "user_id", "used_at"),
    )
