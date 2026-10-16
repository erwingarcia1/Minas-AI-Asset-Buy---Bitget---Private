from __future__ import annotations

from datetime import datetime, timedelta, timezone

from guardian.alerts.digest import build_digest, send_digest
from guardian.alerts.escalation import Escalator
from guardian.channels import build_channels
from tests.guardian.conftest import make_account
from api.config import settings
from tests.guardian.conftest import SHIPPED_RULES


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _insert_alert(db, account_id, severity="CRITICAL", rule="withdrawal_initiated", sent_minutes_ago=30, acked=False):
    sent_at = (datetime.now(timezone.utc) - timedelta(minutes=sent_minutes_ago)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, confidence, sent_at, acknowledged_at) "
            "VALUES (%s, %s, %s, 'k', 'msg', '{}', 'high', %s, %s)",
            (account_id, rule, severity, sent_at, sent_at if acked else None),
        )
        return cur.lastrowid


def test_escalator_refires_unacked_critical(db):
    account_id = make_account(db)
    _insert_alert(db, account_id, sent_minutes_ago=30)
    channel = RecordingChannel()

    escalated = Escalator([channel], unacknowledged_minutes=15).check(db)

    assert escalated == 1
    assert len(channel.sent) == 1
    assert "ESCALATION" in channel.sent[0].rule
    # Second pass must not escalate the same alert again.
    assert Escalator([channel], unacknowledged_minutes=15).check(db) == 0


def test_escalator_leaves_acked_and_recent_alerts_alone(db):
    account_id = make_account(db)
    _insert_alert(db, account_id, sent_minutes_ago=30, acked=True)
    _insert_alert(db, account_id, sent_minutes_ago=2)  # too recent
    channel = RecordingChannel()

    assert Escalator([channel], unacknowledged_minutes=15).check(db) == 0
    assert channel.sent == []


def test_digest_aggregates_medium_low_only(db):
    account_id = make_account(db)
    _insert_alert(db, account_id, severity="MEDIUM", rule="abnormal_fee_spend", sent_minutes_ago=60)
    _insert_alert(db, account_id, severity="LOW", rule="baseline_drift_new_deposit_source", sent_minutes_ago=90)
    _insert_alert(db, account_id, severity="CRITICAL", rule="withdrawal_initiated", sent_minutes_ago=60)

    body = build_digest(db, hours=24)
    assert body is not None
    assert "abnormal_fee_spend" in body
    assert "baseline_drift_new_deposit_source" in body
    assert "withdrawal_initiated" not in body  # CRITICAL never waits for a digest

    channel = RecordingChannel()
    assert send_digest(db, [channel]) is True
    assert len(channel.sent) == 1


def test_empty_digest_is_not_sent(db):
    channel = RecordingChannel()
    assert send_digest(db, [channel]) is False
    assert channel.sent == []


def test_routing_resolves_configured_channels_and_skips_digest(rules, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(settings, "telegram_chat_id", "c")
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "resend_api_key", "")
    channels = build_channels(rules)

    # Test rules.yaml has no routing section -> falls back to empty lists,
    # so use the real config file for this assertion instead.
    from guardian.config import RulesConfig

    real_rules = RulesConfig(SHIPPED_RULES)
    channels = build_channels(real_rules)
    # CRITICAL routes telegram+sms+email; only telegram is configured here.
    assert len(channels["CRITICAL"]) == 1
    # MEDIUM/LOW route only to digest -> no immediate channels.
    assert channels["MEDIUM"] == []
    assert channels["LOW"] == []
