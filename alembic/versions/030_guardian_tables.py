"""Guardian read-only monitoring: 19 guardian_* tables, two users columns (v0.9.1, PR TBD)

Guardian (Minas AI Guardian, the read-only exchange-compromise monitor) moves
into this platform. Its SQLite schema becomes these Postgres tables, prefixed
`guardian_`, with the platform's users table as the identity: every monitored
account belongs to a `users.id`, replacing Guardian's email-string link.

Ids are bigint identity BY DEFAULT, not UUID: Guardian's stored `dedup_key`
hashes, snoozes and cooldowns are keyed on the integer account id, and BY
DEFAULT lets the one-off migration script insert Guardian's original ids (then
setval the sequences) so none of that history silently voids. Timestamps are
timestamptz, JSON payloads are jsonb, balance quantities are numeric(38,18);
`seen_withdrawals.size` stays text because Guardian writes str(amount), which
can be the string 'None'. Column names are Guardian's (payload_json,
evidence_json, ...) so the ported engine SQL stays unchanged.

guardian_accounts additions: user_id (owner), key_fingerprint (sha256 of the
plaintext key, so a purchase key from api_keys can be refused as a monitoring
key and the same key cannot be monitored twice; NULL once a key is scrubbed),
account_mode (Bitget Classic vs Unified, learned at onboarding),
baseline_status (seeding runs in the background after onboarding),
onboarding_source and onboarded_by (self-serve, admin on behalf, or migrated).
Guardian's alert_email is gone: the owner's users.email is the primary
destination; the secondary contacts stay as delivery-only overrides.

Foreign keys cascade from the account, as the platform's own tables do;
operator_pages and sms_deliveries keep their row when the thing they are
about disappears (SET NULL), because a page about our own fault and a
delivery log are not the customer's records.

Not carried over: Guardian's operators, portal_members, their session
secrets, password_resets (the platform has JWT resets) and its TOTP tables
(migration 029 holds the platform-wide user_totp instead).

users.password_reset_required: set for users created without a password
(migrated Guardian members and operators, customers onboarded by an admin).
Login treats them as a wrong password and emails an activation link; the
reset endpoint clears the flag. Existing rows: false.

users.notification_preferences: String(500) -> Text. The JSON blob gains three
Guardian toggles and was one feature away from the limit. Downgrade truncates
nothing in practice (current values are ~230 chars) but is a type change.

All 19 tables start empty. No existing table is read or rewritten.

Revision ID: 030
Revises: 029
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '030'
down_revision: Union[str, None] = '029'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id():
    return sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True)


def _account_id(nullable=False, ondelete='CASCADE'):
    return sa.Column('account_id', sa.BigInteger(),
                     sa.ForeignKey('guardian_accounts.id', ondelete=ondelete), nullable=nullable)


def _ts(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _jsonb(name, nullable=False):
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        'guardian_accounts',
        _id(),
        sa.Column('user_id', sa.UUID(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('exchange', sa.String(length=20), nullable=False),
        sa.Column('topology', sa.String(length=20), nullable=False),
        sa.Column('account_mode', sa.String(length=10), nullable=True),
        sa.Column('encrypted_api_key', sa.Text(), nullable=False),
        sa.Column('encrypted_secret', sa.Text(), nullable=False),
        sa.Column('encrypted_passphrase', sa.Text(), nullable=False),
        sa.Column('key_fingerprint', sa.String(length=64), nullable=True),
        sa.Column('customer_name', sa.String(length=255), nullable=True),
        sa.Column('alert_phone', sa.String(length=32), nullable=True),
        sa.Column('alert_email_secondary', sa.String(length=255), nullable=True),
        sa.Column('alert_phone_secondary', sa.String(length=32), nullable=True),
        sa.Column('telegram_handle', sa.String(length=64), nullable=True),
        sa.Column('telegram_handle_secondary', sa.String(length=64), nullable=True),
        sa.Column('license', sa.String(length=100), nullable=True),
        sa.Column('sms_alerts_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('exchange_uid', sa.String(length=64), nullable=True),
        sa.Column('baseline_status', sa.String(length=12), nullable=False, server_default='seeding'),
        sa.Column('onboarding_source', sa.String(length=10), nullable=False),
        sa.Column('onboarded_by', sa.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        _ts('onboarded_at'),
        _ts('updated_at'),
        sa.UniqueConstraint('label', name='uq_guardian_accounts_label'),
        sa.UniqueConstraint('key_fingerprint', name='uq_guardian_accounts_key_fingerprint'),
        sa.CheckConstraint("exchange IN ('bitget', 'bybit', 'okx', 'binance')", name='ck_guardian_accounts_exchange'),
        sa.CheckConstraint("topology IN ('independent', 'sub_account')", name='ck_guardian_accounts_topology'),
        sa.CheckConstraint("account_mode IN ('classic', 'unified')", name='ck_guardian_accounts_account_mode'),
        sa.CheckConstraint("status IN ('active', 'tamper_suspected', 'disabled')", name='ck_guardian_accounts_status'),
        sa.CheckConstraint("baseline_status IN ('seeding', 'complete', 'incomplete')", name='ck_guardian_accounts_baseline_status'),
        sa.CheckConstraint("onboarding_source IN ('self', 'admin', 'migrated')", name='ck_guardian_accounts_onboarding_source'),
    )
    op.create_index('ix_guardian_accounts_user_id', 'guardian_accounts', ['user_id'])
    op.create_index('ix_guardian_accounts_status', 'guardian_accounts', ['status'])

    op.create_table(
        'guardian_events',
        _id(),
        _account_id(),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False),
        _jsonb('payload_json'),
        sa.Column('actor', sa.String(length=255), nullable=True),
        _ts('created_at'),
    )
    op.create_index('idx_guardian_events_account', 'guardian_events', ['account_id', 'created_at'])

    op.create_table(
        'guardian_known_addresses',
        _id(),
        _account_id(),
        sa.Column('address', sa.String(length=255), nullable=False),
        sa.Column('coin', sa.String(length=32), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False),
        _ts('first_seen_at'),
        sa.UniqueConstraint('account_id', 'address', name='uq_guardian_known_addresses_account_address'),
    )

    op.create_table(
        'guardian_seen_withdrawals',
        _id(),
        _account_id(),
        sa.Column('order_id', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('coin', sa.String(length=32), nullable=True),
        sa.Column('size', sa.Text(), nullable=True),
        sa.Column('address', sa.String(length=255), nullable=True),
        _jsonb('raw_json'),
        _ts('first_seen_at'),
        sa.UniqueConstraint('account_id', 'order_id', name='uq_guardian_seen_withdrawals_account_order'),
    )

    op.create_table(
        'guardian_balance_snapshots',
        _id(),
        _account_id(),
        sa.Column('coin', sa.String(length=32), nullable=False),
        sa.Column('quantity', sa.Numeric(38, 18), nullable=False),
        _ts('snapshot_at'),
    )
    op.create_index('idx_guardian_balance_account_coin', 'guardian_balance_snapshots', ['account_id', 'coin', 'snapshot_at'])
    op.create_index('idx_guardian_balance_account_time', 'guardian_balance_snapshots', ['account_id', 'snapshot_at'])

    op.create_table(
        'guardian_permission_snapshots',
        _id(),
        _account_id(),
        _jsonb('authorities', nullable=True),
        _jsonb('ip_whitelist', nullable=True),
        _ts('snapshot_at'),
    )

    op.create_table(
        'guardian_poll_state',
        sa.Column('account_id', sa.BigInteger(),
                  sa.ForeignKey('guardian_accounts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('consecutive_auth_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        _ts('last_success_at', nullable=True),
        _ts('last_fast_loop_at', nullable=True),
        _ts('last_slow_loop_at', nullable=True),
        # Persisted so the API (another process) can show the breaker and the
        # last error; Guardian kept these in the worker's memory only.
        _ts('circuit_open_until', nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
    )

    op.create_table(
        'guardian_alerts',
        _id(),
        _account_id(),
        sa.Column('rule', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False),
        sa.Column('dedup_key', sa.String(length=128), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        _jsonb('evidence_json'),
        sa.Column('confidence', sa.Text(), nullable=False),   # free text: 'low (proxy - ...)' runs to ~75 chars
        # How many channels accepted it. 0 = detected and reached nobody,
        # which must stay visible as such (Guardian #57).
        sa.Column('delivered_to', sa.Integer(), nullable=False, server_default='0'),
        _ts('sent_at'),
        _ts('acknowledged_at', nullable=True),
        sa.Column('acknowledged_by', sa.String(length=255), nullable=True),
        _ts('escalated_at', nullable=True),
        _ts('snoozed_until', nullable=True),
    )
    op.create_index('idx_guardian_alerts_dedup', 'guardian_alerts', ['account_id', 'dedup_key', 'sent_at'])
    op.create_index('idx_guardian_alerts_account_time', 'guardian_alerts', ['account_id', 'sent_at'])

    op.create_table(
        'guardian_traded_pairs',
        _id(),
        _account_id(),
        sa.Column('symbol', sa.String(length=64), nullable=False),
        _ts('first_seen_at'),
        sa.UniqueConstraint('account_id', 'symbol', name='uq_guardian_traded_pairs_account_symbol'),
    )

    op.create_table(
        'guardian_seen_fills',
        _id(),
        _account_id(),
        sa.Column('fill_key', sa.String(length=128), nullable=False),
        sa.Column('symbol', sa.String(length=64), nullable=True),
        _ts('first_seen_at'),
        sa.UniqueConstraint('account_id', 'fill_key', name='uq_guardian_seen_fills_account_fill'),
    )

    op.create_table(
        'guardian_seen_transfers',
        _id(),
        _account_id(),
        sa.Column('transfer_id', sa.String(length=128), nullable=False),
        _jsonb('raw_json'),
        _ts('first_seen_at'),
        sa.UniqueConstraint('account_id', 'transfer_id', name='uq_guardian_seen_transfers_account_transfer'),
    )

    op.create_table(
        'guardian_transfer_routes',
        _id(),
        _account_id(),
        sa.Column('route', sa.String(length=128), nullable=False),
        _ts('first_seen_at'),
        sa.UniqueConstraint('account_id', 'route', name='uq_guardian_transfer_routes_account_route'),
    )

    # The product reporting its own faults. account_id means "about", never
    # "to"; a page about one account never reaches that customer.
    op.create_table(
        'guardian_operator_pages',
        _id(),
        _account_id(nullable=True, ondelete='SET NULL'),
        sa.Column('rule', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False),
        sa.Column('dedup_key', sa.String(length=128), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        _jsonb('evidence_json'),
        sa.Column('delivered_to', sa.Integer(), nullable=False, server_default='0'),
        _ts('sent_at'),
        _ts('acknowledged_at', nullable=True),
    )
    op.create_index('idx_guardian_operator_pages_dedup', 'guardian_operator_pages', ['dedup_key', 'sent_at'])
    op.create_index('idx_guardian_operator_pages_time', 'guardian_operator_pages', ['sent_at'])

    op.create_table(
        'guardian_fee_baseline',
        sa.Column('account_id', sa.BigInteger(),
                  sa.ForeignKey('guardian_accounts.id', ondelete='CASCADE'), primary_key=True),
        _jsonb('ema_json'),
        _ts('updated_at'),
    )

    op.create_table(
        'guardian_sms_deliveries',
        _id(),
        _account_id(),
        sa.Column('alert_id', sa.BigInteger(),
                  sa.ForeignKey('guardian_alerts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('rule', sa.String(length=128), nullable=False),   # escalations carry an 'ESCALATION-unacknowledged: ' prefix
        sa.Column('phone', sa.String(length=32), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=True),  # the channel class name, e.g. ClickSendSmsChannel
        sa.Column('provider_message_id', sa.String(length=128), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        _ts('created_at'),
        sa.CheckConstraint("status IN ('sent', 'failed', 'skipped', 'suppressed')", name='ck_guardian_sms_deliveries_status'),
    )
    op.create_index('idx_guardian_sms_deliveries_account', 'guardian_sms_deliveries', ['account_id', 'rule', 'created_at'])

    # expires_at NOT NULL on purpose: there is no permanent mute, and the
    # schema is where that is guaranteed. Rows outlive the silence.
    op.create_table(
        'guardian_snoozes',
        _id(),
        _account_id(),
        sa.Column('dedup_key', sa.String(length=128), nullable=False),
        sa.Column('rule', sa.String(length=64), nullable=False),
        sa.Column('requested_by', sa.String(length=255), nullable=False),
        _ts('created_at'),
        _ts('expires_at'),
    )
    op.create_index('idx_guardian_snoozes_lookup', 'guardian_snoozes', ['account_id', 'dedup_key', 'expires_at'])

    # "That was me": one row per marking, kept forever, one mark per alert per
    # person. It records a judgement; it suppresses nothing.
    op.create_table(
        'guardian_expected_activity',
        _id(),
        sa.Column('alert_id', sa.BigInteger(),
                  sa.ForeignKey('guardian_alerts.id', ondelete='CASCADE'), nullable=False),
        _account_id(),
        sa.Column('rule', sa.String(length=64), nullable=False),
        sa.Column('dedup_key', sa.String(length=128), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('marked_by', sa.String(length=255), nullable=False),
        _ts('created_at'),
        sa.UniqueConstraint('alert_id', 'marked_by', name='uq_guardian_expected_activity_alert_marker'),
    )
    op.create_index('idx_guardian_expected_account', 'guardian_expected_activity', ['account_id', 'created_at'])
    op.create_index('idx_guardian_expected_rule', 'guardian_expected_activity', ['rule', 'created_at'])

    # Threshold changes are fleet-wide: no account_id. actor NULL = "we cannot
    # say who", which is itself the finding.
    op.create_table(
        'guardian_config_changes',
        _id(),
        sa.Column('setting', sa.String(length=128), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=True),
        sa.Column('actor', sa.String(length=255), nullable=True),
        _ts('changed_at'),
    )
    op.create_index('idx_guardian_config_changes_time', 'guardian_config_changes', ['changed_at'])

    # Operator triage: a current state, deliberately NOT acknowledged_at, which
    # is the customer's act and what escalation keys on.
    op.create_table(
        'guardian_alert_triage',
        sa.Column('alert_id', sa.BigInteger(),
                  sa.ForeignKey('guardian_alerts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        _ts('handled_at', nullable=True),
        sa.Column('handled_by', sa.String(length=255), nullable=True),
        _ts('updated_at'),
    )
    op.create_index('idx_guardian_triage_handled', 'guardian_alert_triage', ['handled_at'])

    op.add_column('users', sa.Column('password_reset_required', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column('users', 'notification_preferences', existing_type=sa.String(length=500), type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column('users', 'notification_preferences', existing_type=sa.Text(), type_=sa.String(length=500), existing_nullable=True)
    op.drop_column('users', 'password_reset_required')
    for table in (
        'guardian_alert_triage',
        'guardian_config_changes',
        'guardian_expected_activity',
        'guardian_snoozes',
        'guardian_sms_deliveries',
        'guardian_fee_baseline',
        'guardian_operator_pages',
        'guardian_transfer_routes',
        'guardian_seen_transfers',
        'guardian_seen_fills',
        'guardian_traded_pairs',
        'guardian_alerts',
        'guardian_poll_state',
        'guardian_permission_snapshots',
        'guardian_balance_snapshots',
        'guardian_seen_withdrawals',
        'guardian_known_addresses',
        'guardian_events',
        'guardian_accounts',
    ):
        op.drop_table(table)
