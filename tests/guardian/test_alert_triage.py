"""G-37 (#41): the operator's queue across every customer.

The property that matters most here is the one that is easiest to get wrong:
**triage must be invisible to escalation.** `Escalator` decides whether to
escalate a CRITICAL by `acknowledged_at IS NULL` — the customer's state. If
marking an alert "handled" touched that column, an operator tidying their queue
would silently stop the escalation ladder for a customer who has still not
responded. So the first tests below are not about the queue at all; they are
about what triage must never do.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from guardian.alert_triage import muted_channels, open_alerts, set_triage, stale_after_minutes
from guardian.config import RulesConfig
from tests.guardian.conftest import SHIPPED_RULES
from tests.guardian.conftest import make_user

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def rules():
    return RulesConfig(SHIPPED_RULES)


def _account(db, label="acct-1", email="a@example.com"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, label, exchange, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, status, onboarded_at, updated_at) "
            "VALUES (%s, 'self', %s, 'bitget', 'independent', 'x', 'y', 'z', 'active', '2026-01-01', '2026-01-01')",
            (make_user(db, email), label),
        )
        return cur.lastrowid


def _alert(db, account_id, *, severity="CRITICAL", rule="withdrawal_initiated",
           sent_at=NOW, acknowledged=False, delivered_to=1):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, "
            "confidence, delivered_to, sent_at, acknowledged_at) "
            "VALUES (%s, %s, %s, %s, 'msg', '{}', 'high', %s, %s, %s)",
            (account_id, rule, severity, f"{rule}:{sent_at.isoformat()}",
             delivered_to, sent_at.isoformat(),
             NOW.isoformat() if acknowledged else None),
        )
        return cur.lastrowid


# --- what triage must never do ----------------------------------------------

def test_marking_handled_does_not_acknowledge(db):
    """The load-bearing property. Handled is Minas-side bookkeeping; the
    escalation ladder belongs to the customer and must keep running."""
    aid = _account(db)
    alert = _alert(db, aid)

    set_triage(db, alert, handled=True, actor="erwin")

    with db.cursor() as cur:
        cur.execute("SELECT acknowledged_at, acknowledged_by FROM guardian_alerts WHERE id = %s", (alert,))
        row = cur.fetchone()
    assert row["acknowledged_at"] is None, "triage answered for the customer"
    assert row["acknowledged_by"] is None


def test_a_handled_alert_still_appears_to_the_escalator(db, rules):
    """Same property, asserted against the actual consumer: the escalator's
    own query still selects the alert after an operator marks it handled."""
    aid = _account(db)
    old = NOW - timedelta(hours=2)
    alert = _alert(db, aid, sent_at=old)
    set_triage(db, alert, handled=True, actor="erwin")

    with db.cursor() as cur:
        # the shape Escalator uses: unacknowledged CRITICALs
        cur.execute(
            "SELECT id FROM guardian_alerts a WHERE a.severity = 'CRITICAL' AND a.acknowledged_at IS NULL"
        )
        ids = [r["id"] for r in cur.fetchall()]
    assert alert in ids, "a handled alert vanished from the escalator's view"


def test_triage_writes_the_forensic_event(db):
    """The triage row is current state; the events row is the history an
    incident review reads. Both the marker and the note land there, with the
    operator named (G-50)."""
    import json

    aid = _account(db)
    alert = _alert(db, aid)

    set_triage(db, alert, note="called the customer, no answer", handled=True, actor="erwin")

    with db.cursor() as cur:
        cur.execute("SELECT payload_json, actor FROM guardian_events WHERE event_type = 'alert_triaged'")
        row = cur.fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["alert_id"] == alert
    assert payload["handled"] is True
    assert payload["note"] == "called the customer, no answer"
    assert row["actor"] == "erwin"


def test_triage_on_a_missing_alert_refuses(db):
    with pytest.raises(ValueError):
        set_triage(db, 99999, handled=True, actor="erwin")


# --- the queue ---------------------------------------------------------------

def test_the_queue_spans_customers_and_orders_by_consequence(db, rules):
    """A two-hour-old CRITICAL outranks everything MEDIUM regardless of age,
    and within a severity the older alert comes first."""
    a1 = _account(db, "alice-1", "alice@example.com")
    a2 = _account(db, "bob-1", "bob@example.com")
    _alert(db, a1, severity="MEDIUM", rule="abnormal_fee_spend", sent_at=NOW - timedelta(hours=5))
    _alert(db, a2, severity="CRITICAL", rule="withdrawal_initiated", sent_at=NOW - timedelta(hours=2))
    _alert(db, a1, severity="CRITICAL", rule="unexplained_quantity_decrease",
           sent_at=NOW - timedelta(hours=3))

    queue = open_alerts(db, rules, now=NOW)

    assert [(q["severity"], q["label"]) for q in queue] == [
        ("CRITICAL", "alice-1"),   # older CRITICAL first
        ("CRITICAL", "bob-1"),
        ("MEDIUM", "alice-1"),
    ]


def test_an_acknowledged_alert_leaves_the_queue(db, rules):
    """Acknowledged means the customer answered; there is nothing left to
    chase. The queue is defined by their silence, not our bookkeeping."""
    aid = _account(db)
    _alert(db, aid, acknowledged=True)

    assert open_alerts(db, rules, now=NOW) == []


def test_a_handled_alert_is_hidden_from_the_default_view_but_not_gone(db, rules):
    aid = _account(db)
    alert = _alert(db, aid)
    set_triage(db, alert, handled=True, actor="erwin")

    assert open_alerts(db, rules, now=NOW) == []
    shown = open_alerts(db, rules, now=NOW, include_handled=True)
    assert len(shown) == 1
    assert shown[0]["handled"] is True and shown[0]["handled_by"] == "erwin"


def test_overdue_matches_the_escalation_threshold_not_a_literal(db, rules):
    """The queue calls an alert overdue at the same age the escalation ladder
    acts on it. Two different deadlines would teach the operator one the
    product does not keep."""
    threshold = rules.get_int("alerting", "escalation_unacknowledged_minutes")
    assert stale_after_minutes(rules) == threshold

    aid = _account(db)
    _alert(db, aid, sent_at=NOW - timedelta(minutes=threshold + 1))
    _alert(db, aid, rule="withdrawal_burst", sent_at=NOW - timedelta(minutes=max(threshold - 5, 1)))

    queue = open_alerts(db, rules, now=NOW)
    by_rule = {q["rule"]: q for q in queue}
    assert by_rule["withdrawal_initiated"]["overdue"] is True
    assert by_rule["withdrawal_burst"]["overdue"] is False


def test_marking_handled_does_not_clear_overdue(db, rules):
    """Overdue is about the customer not having responded. Ticking our box
    does not answer for them."""
    aid = _account(db)
    alert = _alert(db, aid, sent_at=NOW - timedelta(hours=2))
    set_triage(db, alert, handled=True, actor="erwin")

    shown = open_alerts(db, rules, now=NOW, include_handled=True)
    assert shown[0]["overdue"] is True


def test_delivered_to_nobody_is_flagged(db, rules):
    """detected-but-reached-nobody is a different failure from not-detected
    (#57), and the queue is where an operator can actually see it."""
    aid = _account(db)
    _alert(db, aid, delivered_to=0)

    queue = open_alerts(db, rules, now=NOW)
    assert queue[0]["delivered_to"] == 0


def test_a_reopened_alert_returns_to_the_queue(db, rules):
    aid = _account(db)
    alert = _alert(db, aid)
    set_triage(db, alert, handled=True, actor="erwin")
    set_triage(db, alert, handled=False, actor="erwin")

    queue = open_alerts(db, rules, now=NOW)
    assert len(queue) == 1
    assert queue[0]["handled"] is False


# --- muted channels ----------------------------------------------------------

def test_live_snoozes_and_sms_optouts_are_surfaced(db):
    a1 = _account(db, "alice-1", "alice@example.com")
    a2 = _account(db, "bob-1", "bob@example.com")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_snoozes (account_id, dedup_key, rule, requested_by, created_at, expires_at) "
            "VALUES (%s, 'k', 'abnormal_fee_spend', 'alice@example.com', %s, %s)",
            (a1, NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()),
        )
        cur.execute("UPDATE guardian_accounts SET sms_alerts_enabled = false WHERE id = %s", (a2,))

    muted = muted_channels(db, now=NOW)

    by_label = {m["label"]: m for m in muted}
    assert by_label["alice-1"]["snoozed_rules"] == ["abnormal_fee_spend"]
    assert by_label["bob-1"]["sms_off"] is True


def test_an_expired_snooze_is_not_a_grievance(db):
    """Only live silence is a signal. An expired snooze sitting on this list
    forever would train the operator to ignore the list."""
    a1 = _account(db, "alice-1", "alice@example.com")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_snoozes (account_id, dedup_key, rule, requested_by, created_at, expires_at) "
            "VALUES (%s, 'k', 'abnormal_fee_spend', 'alice@example.com', %s, %s)",
            (a1, (NOW - timedelta(days=2)).isoformat(), (NOW - timedelta(days=1)).isoformat()),
        )

    assert muted_channels(db, now=NOW) == []


# --- the route ---------------------------------------------------------------

