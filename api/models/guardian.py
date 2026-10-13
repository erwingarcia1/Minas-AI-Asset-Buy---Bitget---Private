"""Guardian read-only monitoring tables (migration 030, v0.9.1).

The ORM mirror of the 19 `guardian_*` tables. The Guardian worker writes them
through its own raw-SQL wrapper (guardian/db.py); the API reads them through
these models. Alembic migration 030 is the schema's source of truth and a
test asserts this metadata matches it, so the two cannot drift.

Ids are bigint identity (not UUID): the engine's dedup keys and snoozes are
keyed on the integer account id and the cutover keeps Guardian's original ids.
Column names are Guardian's (payload_json, evidence_json, ...) so the ported
engine SQL is unchanged.
"""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Identity, Index,
                        Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from api.database import Base

GUARDIAN_EXCHANGES = ("bitget", "bybit", "okx", "binance")
GUARDIAN_TOPOLOGIES = ("independent", "sub_account")
GUARDIAN_ACCOUNT_STATUSES = ("active", "tamper_suspected", "disabled")
GUARDIAN_BASELINE_STATUSES = ("seeding", "complete", "incomplete")
GUARDIAN_ONBOARDING_SOURCES = ("self", "admin", "migrated")


def _id():
    return Column(BigInteger, Identity(always=False), primary_key=True)


def _account_id(nullable=False, ondelete="CASCADE"):
    return Column(BigInteger, ForeignKey("guardian_accounts.id", ondelete=ondelete), nullable=nullable)


def _ts(nullable=False):
    return Column(DateTime(timezone=True), nullable=nullable)


def _jsonb(nullable=False):
    return Column(JSONB(astext_type=Text()), nullable=nullable)


def _in(values):
    return ", ".join(f"'{v}'" for v in values)


class GuardianAccount(Base):
    __tablename__ = "guardian_accounts"

    id = _id()
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(100), nullable=False)
    exchange = Column(String(20), nullable=False)
    topology = Column(String(20), nullable=False)
    account_mode = Column(String(10), nullable=True)
    encrypted_api_key = Column(Text, nullable=False)
    encrypted_secret = Column(Text, nullable=False)
    encrypted_passphrase = Column(Text, nullable=False)
    key_fingerprint = Column(String(64), nullable=True)
    customer_name = Column(String(255), nullable=True)
    alert_phone = Column(String(32), nullable=True)
    alert_email_secondary = Column(String(255), nullable=True)
    alert_phone_secondary = Column(String(32), nullable=True)
    telegram_handle = Column(String(64), nullable=True)
    telegram_handle_secondary = Column(String(64), nullable=True)
    license = Column(String(100), nullable=True)
    sms_alerts_enabled = Column(Boolean, nullable=False, server_default="true")
    status = Column(String(20), nullable=False, server_default="active")
    exchange_uid = Column(String(64), nullable=True)
    baseline_status = Column(String(12), nullable=False, server_default="seeding")
    onboarding_source = Column(String(10), nullable=False)
    onboarded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    onboarded_at = _ts()
    updated_at = _ts()

    __table_args__ = (
        UniqueConstraint("label", name="uq_guardian_accounts_label"),
        UniqueConstraint("key_fingerprint", name="uq_guardian_accounts_key_fingerprint"),
        CheckConstraint(f"exchange IN ({_in(GUARDIAN_EXCHANGES)})", name="ck_guardian_accounts_exchange"),
        CheckConstraint(f"topology IN ({_in(GUARDIAN_TOPOLOGIES)})", name="ck_guardian_accounts_topology"),
        CheckConstraint("account_mode IN ('classic', 'unified')", name="ck_guardian_accounts_account_mode"),
        CheckConstraint(f"status IN ({_in(GUARDIAN_ACCOUNT_STATUSES)})", name="ck_guardian_accounts_status"),
        CheckConstraint(f"baseline_status IN ({_in(GUARDIAN_BASELINE_STATUSES)})", name="ck_guardian_accounts_baseline_status"),
        CheckConstraint(f"onboarding_source IN ({_in(GUARDIAN_ONBOARDING_SOURCES)})", name="ck_guardian_accounts_onboarding_source"),
        Index("ix_guardian_accounts_user_id", "user_id"),
        Index("ix_guardian_accounts_status", "status"),
    )


class GuardianEvent(Base):
    __tablename__ = "guardian_events"

    id = _id()
    account_id = _account_id()
    event_type = Column(String(64), nullable=False)
    severity = Column(String(10), nullable=False)
    payload_json = _jsonb()
    actor = Column(String(255), nullable=True)
    created_at = _ts()

    __table_args__ = (Index("idx_guardian_events_account", "account_id", "created_at"),)


class GuardianKnownAddress(Base):
    __tablename__ = "guardian_known_addresses"

    id = _id()
    account_id = _account_id()
    address = Column(String(255), nullable=False)
    coin = Column(String(32), nullable=True)
    source = Column(String(32), nullable=False)
    first_seen_at = _ts()

    __table_args__ = (UniqueConstraint("account_id", "address", name="uq_guardian_known_addresses_account_address"),)


class GuardianSeenWithdrawal(Base):
    __tablename__ = "guardian_seen_withdrawals"

    id = _id()
    account_id = _account_id()
    order_id = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False)
    coin = Column(String(32), nullable=True)
    size = Column(Text, nullable=True)
    address = Column(String(255), nullable=True)
    raw_json = _jsonb()
    first_seen_at = _ts()

    __table_args__ = (UniqueConstraint("account_id", "order_id", name="uq_guardian_seen_withdrawals_account_order"),)


class GuardianBalanceSnapshot(Base):
    __tablename__ = "guardian_balance_snapshots"

    id = _id()
    account_id = _account_id()
    coin = Column(String(32), nullable=False)
    quantity = Column(Numeric(38, 18), nullable=False)
    snapshot_at = _ts()

    __table_args__ = (
        Index("idx_guardian_balance_account_coin", "account_id", "coin", "snapshot_at"),
        Index("idx_guardian_balance_account_time", "account_id", "snapshot_at"),
    )


class GuardianPermissionSnapshot(Base):
    __tablename__ = "guardian_permission_snapshots"

    id = _id()
    account_id = _account_id()
    authorities = _jsonb(nullable=True)
    ip_whitelist = _jsonb(nullable=True)
    snapshot_at = _ts()


class GuardianPollState(Base):
    __tablename__ = "guardian_poll_state"

    account_id = Column(BigInteger, ForeignKey("guardian_accounts.id", ondelete="CASCADE"), primary_key=True)
    consecutive_auth_failures = Column(Integer, nullable=False, server_default="0")
    consecutive_failures = Column(Integer, nullable=False, server_default="0")
    last_success_at = _ts(nullable=True)
    last_fast_loop_at = _ts(nullable=True)
    last_slow_loop_at = _ts(nullable=True)
    circuit_open_until = _ts(nullable=True)
    last_error = Column(Text, nullable=True)


class GuardianAlert(Base):
    __tablename__ = "guardian_alerts"

    id = _id()
    account_id = _account_id()
    rule = Column(String(64), nullable=False)
    severity = Column(String(10), nullable=False)
    dedup_key = Column(String(128), nullable=False)
    message = Column(Text, nullable=False)
    evidence_json = _jsonb()
    confidence = Column(Text, nullable=False)
    delivered_to = Column(Integer, nullable=False, server_default="0")
    sent_at = _ts()
    acknowledged_at = _ts(nullable=True)
    acknowledged_by = Column(String(255), nullable=True)
    escalated_at = _ts(nullable=True)
    snoozed_until = _ts(nullable=True)

    __table_args__ = (
        Index("idx_guardian_alerts_dedup", "account_id", "dedup_key", "sent_at"),
        Index("idx_guardian_alerts_account_time", "account_id", "sent_at"),
    )


class GuardianTradedPair(Base):
    __tablename__ = "guardian_traded_pairs"

    id = _id()
    account_id = _account_id()
    symbol = Column(String(64), nullable=False)
    first_seen_at = _ts()

    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_guardian_traded_pairs_account_symbol"),)


class GuardianSeenFill(Base):
    __tablename__ = "guardian_seen_fills"

    id = _id()
    account_id = _account_id()
    fill_key = Column(String(128), nullable=False)
    symbol = Column(String(64), nullable=True)
    first_seen_at = _ts()

    __table_args__ = (UniqueConstraint("account_id", "fill_key", name="uq_guardian_seen_fills_account_fill"),)


class GuardianSeenTransfer(Base):
    __tablename__ = "guardian_seen_transfers"

    id = _id()
    account_id = _account_id()
    transfer_id = Column(String(128), nullable=False)
    raw_json = _jsonb()
    first_seen_at = _ts()

    __table_args__ = (UniqueConstraint("account_id", "transfer_id", name="uq_guardian_seen_transfers_account_transfer"),)


class GuardianTransferRoute(Base):
    __tablename__ = "guardian_transfer_routes"

    id = _id()
    account_id = _account_id()
    route = Column(String(128), nullable=False)
    first_seen_at = _ts()

    __table_args__ = (UniqueConstraint("account_id", "route", name="uq_guardian_transfer_routes_account_route"),)


class GuardianOperatorPage(Base):
    __tablename__ = "guardian_operator_pages"

    id = _id()
    account_id = _account_id(nullable=True, ondelete="SET NULL")
    rule = Column(String(64), nullable=False)
    severity = Column(String(10), nullable=False)
    dedup_key = Column(String(128), nullable=False)
    message = Column(Text, nullable=False)
    evidence_json = _jsonb()
    delivered_to = Column(Integer, nullable=False, server_default="0")
    sent_at = _ts()
    acknowledged_at = _ts(nullable=True)

    __table_args__ = (
        Index("idx_guardian_operator_pages_dedup", "dedup_key", "sent_at"),
        Index("idx_guardian_operator_pages_time", "sent_at"),
    )


class GuardianFeeBaseline(Base):
    __tablename__ = "guardian_fee_baseline"

    account_id = Column(BigInteger, ForeignKey("guardian_accounts.id", ondelete="CASCADE"), primary_key=True)
    ema_json = _jsonb()
    updated_at = _ts()


class GuardianSmsDelivery(Base):
    __tablename__ = "guardian_sms_deliveries"

    id = _id()
    account_id = _account_id()
    alert_id = Column(BigInteger, ForeignKey("guardian_alerts.id", ondelete="SET NULL"), nullable=True)
    rule = Column(String(128), nullable=False)
    phone = Column(String(32), nullable=True)
    provider = Column(String(32), nullable=True)
    provider_message_id = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = _ts()

    __table_args__ = (
        CheckConstraint("status IN ('sent', 'failed', 'skipped', 'suppressed')", name="ck_guardian_sms_deliveries_status"),
        Index("idx_guardian_sms_deliveries_account", "account_id", "rule", "created_at"),
    )


class GuardianSnooze(Base):
    __tablename__ = "guardian_snoozes"

    id = _id()
    account_id = _account_id()
    dedup_key = Column(String(128), nullable=False)
    rule = Column(String(64), nullable=False)
    requested_by = Column(String(255), nullable=False)
    created_at = _ts()
    expires_at = _ts()

    __table_args__ = (Index("idx_guardian_snoozes_lookup", "account_id", "dedup_key", "expires_at"),)


class GuardianExpectedActivity(Base):
    __tablename__ = "guardian_expected_activity"

    id = _id()
    alert_id = Column(BigInteger, ForeignKey("guardian_alerts.id", ondelete="CASCADE"), nullable=False)
    account_id = _account_id()
    rule = Column(String(64), nullable=False)
    dedup_key = Column(String(128), nullable=False)
    note = Column(Text, nullable=True)
    marked_by = Column(String(255), nullable=False)
    created_at = _ts()

    __table_args__ = (
        UniqueConstraint("alert_id", "marked_by", name="uq_guardian_expected_activity_alert_marker"),
        Index("idx_guardian_expected_account", "account_id", "created_at"),
        Index("idx_guardian_expected_rule", "rule", "created_at"),
    )


class GuardianConfigChange(Base):
    __tablename__ = "guardian_config_changes"

    id = _id()
    setting = Column(String(128), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    actor = Column(String(255), nullable=True)
    changed_at = _ts()

    __table_args__ = (Index("idx_guardian_config_changes_time", "changed_at"),)


class GuardianAlertTriage(Base):
    __tablename__ = "guardian_alert_triage"

    alert_id = Column(BigInteger, ForeignKey("guardian_alerts.id", ondelete="CASCADE"), primary_key=True)
    note = Column(Text, nullable=False, server_default="")
    handled_at = _ts(nullable=True)
    handled_by = Column(String(255), nullable=True)
    updated_at = _ts()

    __table_args__ = (Index("idx_guardian_triage_handled", "handled_at"),)
