"""tests/guardian: Guardian's engine suite, ported onto the platform (v0.9.1).

The engine, the detectors, dispatch, retention, escalation, the contact
router and the onboarding gates are Guardian's code with the storage swapped
for Postgres; these tests are the proof that detection did not change in the
move. They run against the same throwaway `*_test` database the rest of the
suite uses (tests/conftest.py creates it and migrates it to head), on
Guardian's own fixtures:

* `db` — a `guardian.db.Database` on the test database. Every `guardian_*`
  table is truncated before and after each test (`RESTART IDENTITY`, so ids
  are deterministic, which many assertions rely on). The engine commits per
  cursor and spawns threads with their own connections, so transaction
  rollback is not an option here.
* `make_account` — inserts a monitored account under the one test user this
  module owns (`guardian-tests@example.com`, CLAUDE.md's local throwaway
  convention).
* `cipher`, `rules` — as Guardian's.
* `safe_settings` (autouse) — every transport setting blanked, so nothing
  here can reach Telegram, Resend, ClickSend or Twilio; a test that wants a
  transport replaces `guardian.net.post` or the channel, as before.

No environment wiping: the platform's root conftest owns DATABASE_URL and
REDIS_URL, and the worker reads configuration from `api.config.settings`,
never from the environment.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from api.config import settings
from guardian.config import RulesConfig
from guardian.crypto import CredentialCipher
from guardian.db import Database, platform_dsn

RULES_YAML = """
polling:
  fast_loop_seconds: "45"
  slow_loop_seconds: "600"
  jitter_seconds: "10"
detectors:
  quantity_drop:
    tolerance_pct: "0.5"
    tolerance_absolute: "0.00000001"
  tamper:
    consecutive_auth_failures_threshold: 2
  heartbeat:
    consecutive_failures_threshold: 5
alerting:
  cooldown_minutes:
    CRITICAL: "15"
    HIGH: "30"
    MEDIUM: "720"
    LOW: "1440"
  escalation_unacknowledged_minutes: "15"
"""

TEST_USER_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0a0")
TEST_USER_EMAIL = "guardian-tests@example.com"

GUARDIAN_TABLES = (
    "guardian_alert_triage", "guardian_config_changes", "guardian_expected_activity", "guardian_snoozes",
    "guardian_sms_deliveries", "guardian_fee_baseline", "guardian_operator_pages", "guardian_transfer_routes",
    "guardian_seen_transfers", "guardian_seen_fills", "guardian_traded_pairs", "guardian_alerts",
    "guardian_poll_state", "guardian_permission_snapshots", "guardian_balance_snapshots",
    "guardian_seen_withdrawals", "guardian_known_addresses", "guardian_events", "guardian_accounts",
)

TRANSPORT_SETTINGS = (
    "telegram_bot_token", "telegram_chat_id", "guardian_telegram_chat_id", "resend_api_key", "resend_from_email",
    "twilio_account_sid", "twilio_auth_token", "twilio_from_number", "twilio_to_numbers",
    "clicksend_username", "clicksend_api_key", "clicksend_from", "clicksend_to_numbers",
    "app_base_url", "slack_webhook_url",
)


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    """No test may see a real transport credential, whatever is in the shell."""
    for attr in TRANSPORT_SETTINGS:
        monkeypatch.setattr(settings, attr, "")
    monkeypatch.setattr(settings, "guardian_enabled", True)


def _truncate(cur) -> None:
    cur.execute("TRUNCATE " + ", ".join(GUARDIAN_TABLES) + " RESTART IDENTITY CASCADE")


def _ensure_test_user(cur) -> None:
    cur.execute(
        "INSERT INTO users (id, email, password_hash, kyc_status, role, suspended, phone_verified, "
        "created_at, updated_at) VALUES (%s, %s, 'x', 'verified', 'user', false, false, now(), now()) "
        "ON CONFLICT (id) DO NOTHING",
        (TEST_USER_ID, TEST_USER_EMAIL),
    )


@pytest.fixture
def db():
    database = Database(platform_dsn(os.environ["DATABASE_URL"]))
    with database.cursor() as cur:
        _truncate(cur)
        _ensure_test_user(cur)
    yield database
    try:
        with database.cursor() as cur:
            _truncate(cur)
    finally:
        database.close()


@pytest.fixture
def cipher():
    return CredentialCipher()


@pytest.fixture
def rules(tmp_path):
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(RULES_YAML)
    return RulesConfig(rules_path)


def make_account(db, label="acct-1", *, user_id=TEST_USER_ID, exchange="bitget"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts "
            "(user_id, label, exchange, topology, encrypted_api_key, encrypted_secret, encrypted_passphrase, "
            "onboarding_source, onboarded_at, updated_at) "
            "VALUES (%s, %s, %s, 'independent', 'x', 'y', 'z', 'self', '2026-01-01', '2026-01-01')",
            (user_id, label, exchange),
        )
        return cur.lastrowid


def make_user(db, email: str, *, role: str = "user") -> uuid.UUID:
    """A platform user for tests about ownership and contacts. Users are not
    truncated between tests (they are the platform's table), so this is
    get-or-create by email."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email.lower(),))
        row = cur.fetchone()
        if row is not None:
            return row["id"]
        user_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO users (id, email, password_hash, kyc_status, role, suspended, phone_verified, "
            "created_at, updated_at) VALUES (%s, %s, 'x', 'verified', %s, false, false, now(), now())",
            (user_id, email.lower(), role),
        )
    return user_id


SHIPPED_RULES = str(Path(__file__).resolve().parents[2] / "guardian" / "rules.yaml")
