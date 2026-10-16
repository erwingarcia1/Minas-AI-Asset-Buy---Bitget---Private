"""Member SMS notifications through ClickSend: body content, delivery
receipts, the sms_deliveries audit log, duplicate suppression, member
preference handling, and missing-phone handling."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from guardian.alerts.base import AlertMessage
from guardian.alerts.clicksend import ClickSendSmsChannel
from guardian.alerts.contact import ContactRouter
from guardian.alerts.dispatch import dispatch
from guardian.alerts.sms import format_sms_body
from guardian.config import RulesConfig
from guardian.detectors.base import Detection
from tests.guardian.conftest import make_account
from api.config import settings


def _alert(message="0.5 BTC withdrawal", severity="CRITICAL", rule="withdrawal_initiated"):
    return AlertMessage(account_label="acct-1", rule=rule, severity=severity,
                        confidence="high", message=message, evidence={})


def _detection(severity="CRITICAL", rule="withdrawal_initiated", order_id="wd-1"):
    return Detection(rule=rule, severity=severity, confidence="high",
                     message="0.5 BTC withdrawal", evidence={"order_id": order_id})


def _stored_alert(db, account_id, detection) -> int:
    """Store a real alerts row (sms_deliveries.alert_id is a foreign key)."""
    return dispatch(db, {}, account_id, "acct-1", detection, {"CRITICAL": 15, "HIGH": 30})


class _StubSms:
    """Recording SMS channel returning ClickSend-shaped receipts."""

    instances: list = []
    fail_with: Exception | None = None

    def __init__(self, recipients):
        self.recipients = recipients
        self.sent: list = []
        _StubSms.instances.append(self)

    def send(self, alert):
        if _StubSms.fail_with is not None:
            raise _StubSms.fail_with
        self.sent.append(alert)
        return [
            {"to": r, "provider": "clicksend", "provider_message_id": f"CS-{i}", "status": "sent"}
            for i, r in enumerate(self.recipients)
        ]


def _router(db, rules, monkeypatch, fail_with=None):
    _StubSms.instances = []
    _StubSms.fail_with = fail_with
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(
        "guardian.alerts.contact.build_sms_channel",
        lambda recipients: _StubSms(recipients) if recipients else None,
    )
    return ContactRouter(db=db, rules=rules)


def _deliveries(db, account_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM guardian_sms_deliveries WHERE account_id = %s ORDER BY id", (account_id,)
        )
        return [dict(r) for r in cur.fetchall()]


# --- AC2/AC3: body content and length ---------------------------------------

def test_sms_body_has_name_severity_rule_account_and_login_link(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://guardian.example.com")
    body = format_sms_body(_alert())
    assert "Minas AI Guardian" in body
    assert "CRITICAL" in body
    assert "withdrawal_initiated" in body
    assert "acct-1" in body
    assert "0.5 BTC withdrawal" in body
    assert body.endswith("Log in now to review: https://guardian.example.com/guardian")


def test_sms_body_has_login_instruction_without_portal_url(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "")
    body = format_sms_body(_alert())
    assert body.endswith("Log in to Minas AI Guardian now to review.")


def test_sms_body_truncation_never_cuts_the_login_link(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://guardian.example.com")
    body = format_sms_body(_alert(message="x" * 600))
    assert len(body) <= 320
    assert body.endswith("Log in now to review: https://guardian.example.com/guardian")
    assert "..." in body  # detail was truncated, not the action


# --- AC5: ClickSend receipts capture the provider message id -----------------

def test_clicksend_send_returns_receipts_with_message_ids():
    with patch("guardian.net.post") as post:
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "response_code": "SUCCESS",
                "data": {"messages": [
                    {"status": "SUCCESS", "message_id": "B0DD8A16-worker1", "to": "+16477216549"},
                ]},
            },
        )
        receipts = ClickSendSmsChannel("u", "k", None, ["+16477216549"]).send(_alert())
    assert receipts == [
        {"to": "+16477216549", "provider": "clicksend",
         "provider_message_id": "B0DD8A16-worker1", "status": "sent"}
    ]


# --- AC1/AC5: sent SMS is recorded with alert id, phone, provider id ---------

def test_sent_sms_recorded_in_delivery_log(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    detection = _detection()
    alert_id = _stored_alert(db, account_id, detection)

    router.send(detection, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=alert_id)

    rows = _deliveries(db, account_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["alert_id"] == alert_id
    assert rows[0]["phone"] == "+15551112222"
    assert rows[0]["provider"] == "clicksend"
    assert rows[0]["provider_message_id"] == "CS-0"
    assert rows[0]["created_at"]


# --- AC10: delivery failure is logged for admin review ------------------------

def test_failed_sms_recorded_with_provider_error(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch, fail_with=RuntimeError("ClickSend failed to queue: INVALID_RECIPIENT"))
    account_id = make_account(db)
    detection = _detection()
    alert_id = _stored_alert(db, account_id, detection)

    # Must not raise: delivery failure never stalls detection.
    router.send(detection, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=alert_id)

    rows = _deliveries(db, account_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["alert_id"] == alert_id
    assert rows[0]["phone"] == "+15551112222"
    assert "INVALID_RECIPIENT" in rows[0]["detail"]


# --- AC7: duplicate SMS suppression -------------------------------------------

def test_duplicate_sms_suppressed_within_window(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    d1 = _detection(order_id="wd-1")
    d2 = _detection(order_id="wd-2")  # different withdrawal, same rule
    a1 = _stored_alert(db, account_id, d1)
    a2 = _stored_alert(db, account_id, d2)

    router.send(d1, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=a1)
    router.send(d2, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=a2)

    rows = _deliveries(db, account_id)
    assert [r["status"] for r in rows] == ["sent", "suppressed"]
    assert sum(len(i.sent) for i in _StubSms.instances) == 1  # one text, not two


def test_sms_sends_again_after_window_expires(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    d1, d2 = _detection(order_id="wd-1"), _detection(order_id="wd-2")
    a1 = _stored_alert(db, account_id, d1)
    a2 = _stored_alert(db, account_id, d2)

    router.send(d1, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=a1)
    # Age the sent row past the 30-minute default window.
    stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_sms_deliveries SET created_at = %s", (stale,))
    router.send(d2, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=a2)

    rows = _deliveries(db, account_id)
    assert [r["status"] for r in rows] == ["sent", "sent"]


# --- AC8: member preference, with the CRITICAL business-rule override ---------

def _set_pref(db, account_id, enabled: int):
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET sms_alerts_enabled = %s WHERE id = %s", (bool(enabled), account_id))


def test_optout_skips_noncritical_sms(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    _set_pref(db, account_id, 0)
    detection = _detection(severity="HIGH", rule="withdrawal_burst")
    alert_id = _stored_alert(db, account_id, detection)

    router.send(detection, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=alert_id)

    rows = _deliveries(db, account_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert "preference" in rows[0]["detail"]
    assert not _StubSms.instances


def test_critical_overrides_optout_by_default(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    _set_pref(db, account_id, 0)
    detection = _detection(severity="CRITICAL")
    alert_id = _stored_alert(db, account_id, detection)

    router.send(detection, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=alert_id)

    rows = _deliveries(db, account_id)
    assert [r["status"] for r in rows] == ["sent"]


def test_optout_is_absolute_when_override_disabled(db, tmp_path, monkeypatch):
    rules_path = tmp_path / "rules-no-override.yaml"
    rules_path.write_text(
        "alerting:\n  sms_cooldown_minutes: \"30\"\n  sms_critical_overrides_optout: false\n"
    )
    router = _router(db, RulesConfig(rules_path), monkeypatch)
    account_id = make_account(db)
    _set_pref(db, account_id, 0)
    detection = _detection(severity="CRITICAL")
    alert_id = _stored_alert(db, account_id, detection)

    router.send(detection, "acct-1", [], ["+15551112222"], account_id=account_id, alert_id=alert_id)

    rows = _deliveries(db, account_id)
    assert [r["status"] for r in rows] == ["skipped"]
    assert not _StubSms.instances


# --- AC9: missing phone number is logged, not silently dropped ----------------

def test_critical_with_no_phone_records_skip(db, rules, monkeypatch, caplog):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    detection = _detection(severity="CRITICAL")
    alert_id = _stored_alert(db, account_id, detection)

    with caplog.at_level("WARNING", logger="guardian.alerts.contact"):
        router.send(detection, "acct-1", [], [], account_id=account_id, alert_id=alert_id)

    rows = _deliveries(db, account_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert rows[0]["detail"] == "no phone number on file"
    assert any("no SMS destination" in r.message for r in caplog.records)
    assert not _StubSms.instances


def test_noncritical_with_no_phone_records_nothing(db, rules, monkeypatch):
    router = _router(db, rules, monkeypatch)
    account_id = make_account(db)
    detection = _detection(severity="HIGH", rule="withdrawal_burst")
    alert_id = _stored_alert(db, account_id, detection)

    router.send(detection, "acct-1", [], [], account_id=account_id, alert_id=alert_id)

    assert _deliveries(db, account_id) == []


# --- AC4: SMS only goes to the owning account's phone -------------------------

def test_sms_goes_only_to_owning_accounts_phone(db, cipher, rules, monkeypatch):
    from guardian.poller import AccountPoller

    account_a = make_account(db, "member-a")
    account_b = make_account(db, "member-b")
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET alert_phone = '+15550001111' WHERE id = %s", (account_a,))
        cur.execute("UPDATE guardian_accounts SET alert_phone = '+15559998888' WHERE id = %s", (account_b,))

    router = _router(db, rules, monkeypatch)
    poller = AccountPoller(db, cipher, rules, {}, contact_router=router)
    poller._emit(account_a, "member-a", _detection())

    assert len(_StubSms.instances) == 1
    assert _StubSms.instances[0].recipients == ["+15550001111"]
    assert _deliveries(db, account_b) == []
    rows = _deliveries(db, account_a)
    assert len(rows) == 1 and rows[0]["phone"] == "+15550001111"
