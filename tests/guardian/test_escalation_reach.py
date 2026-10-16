"""Who an escalation actually reaches (G-23, #27).

Two of this issue's four criteria were unbuilt, and both are about *reach*
rather than timing. The timings themselves — how long is "a set time", what
makes a channel "louder" — are the product decision this issue is otherwise
blocked on, and are untouched here.

The gap that matters: escalation went to the **global** severity channels, the
same ones the original alert used. So an unacknowledged CRITICAL re-fired down
exactly the path that had already failed to get a response, and the customer's
own contacts — including the secondary contact G-21 exists to provide — never
saw it. Escalation that repeats the unanswered channel is not escalation.

The second: the operator was paged only when escalation *failed to deliver*
(#58). But an escalation happening at all means a customer has not answered a
CRITICAL, which is a fact Minas needs regardless of whether the message left
the building.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from guardian.alerts.escalation import Escalator
from tests.guardian.conftest import TEST_USER_ID, make_user


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class SpyRouter:
    def __init__(self):
        self.calls = []

    def send(self, detection, account_label, emails, phones, *, account_id=None, alert_id=None):
        self.calls.append({"emails": list(emails or []), "phones": list(phones or []),
                           "account_id": account_id, "rule": detection.rule,
                           "severity": detection.severity})


class SpyAlerter:
    def __init__(self):
        self.calls = []

    def page(self, **kwargs):
        self.calls.append(kwargs)


def _account(db, **contacts):
    """`alert_email` is the OWNER's email now (the platform's primary alert
    destination comes from users.email); the other contacts are the account's
    own overrides, as before."""
    fields = {"alert_email": None, "alert_phone": None,
              "alert_email_secondary": None, "alert_phone_secondary": None}
    fields.update(contacts)
    owner = make_user(db, fields["alert_email"]) if fields["alert_email"] else TEST_USER_ID
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, label, exchange, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, alert_phone, alert_email_secondary, "
            "alert_phone_secondary, status, onboarded_at, updated_at) "
            "VALUES (%s, 'self', 'acct-1', 'bitget', 'independent', 'x', 'y', 'z', %s, %s, %s, 'active', "
            "'2026-01-01', '2026-01-01')",
            (owner, fields["alert_phone"],
             fields["alert_email_secondary"], fields["alert_phone_secondary"]),
        )
        account_id = cur.lastrowid
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))
    return account_id


def _stale_critical(db, account_id, minutes_ago=30):
    sent_at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, "
            "confidence, sent_at) VALUES (%s, 'withdrawal_initiated', 'CRITICAL', 'k', "
            "'0.5 BTC left the account', '{}', 'high', %s)",
            (account_id, sent_at),
        )
        return cur.lastrowid


# --- the customer's own contacts, including the second pair of eyes ----------


def test_escalation_reaches_the_secondary_contact(db):
    """The criterion, and the reason it matters: the primary contact is the
    person who has already not answered. G-21 exists so there is somebody else,
    and escalation is precisely the moment to use them."""
    account_id = _account(db, alert_email="primary@example.com",
                          alert_email_secondary="second@example.com",
                          alert_phone_secondary="+15550002")
    _stale_critical(db, account_id)
    router = SpyRouter()

    Escalator([Recording()], unacknowledged_minutes=15, contact_router=router).check(db)

    assert len(router.calls) == 1
    assert "second@example.com" in router.calls[0]["emails"]
    assert "+15550002" in router.calls[0]["phones"]
    assert "primary@example.com" in router.calls[0]["emails"]  # both, not instead


def test_an_account_with_no_contacts_does_not_break_escalation(db):
    """Most accounts have no per-account contacts at all — they are reached
    through the global channels. Escalation must still happen for them."""
    account_id = _account(db)
    _stale_critical(db, account_id)
    channel = Recording()

    escalated = Escalator([channel], unacknowledged_minutes=15,
                          contact_router=SpyRouter()).check(db)

    assert escalated == 1
    assert len(channel.sent) == 1


def test_the_escalation_the_contacts_receive_is_marked_as_an_escalation(db):
    """Not a copy of the original alert. Someone reading it needs to know this
    is the second time, not the first — otherwise the escalation is
    indistinguishable from the message they already ignored."""
    account_id = _account(db, alert_email="primary@example.com")
    _stale_critical(db, account_id)
    router = SpyRouter()

    Escalator([Recording()], unacknowledged_minutes=15, contact_router=router).check(db)

    assert "ESCALATION" in router.calls[0]["rule"]
    assert router.calls[0]["severity"] == "CRITICAL"
    assert router.calls[0]["account_id"] == account_id


def test_a_broken_contact_router_does_not_stop_the_global_escalation(db):
    """Same rule as #57 everywhere else: one delivery path failing must not
    cost the others. The router reaches the customer's own address; the
    channels reach whatever else is configured. Losing both because one raised
    is the failure this codebase keeps having."""
    account_id = _account(db, alert_email="primary@example.com")
    _stale_critical(db, account_id)

    class Exploding:
        def send(self, *a, **k):
            raise RuntimeError("smtp is down")

    channel = Recording()
    escalated = Escalator([channel], unacknowledged_minutes=15,
                          contact_router=Exploding()).check(db)

    assert escalated == 1
    assert len(channel.sent) == 1


# --- Minas is told an escalation happened at all -----------------------------


def test_every_escalation_pages_the_operator(db):
    """Previously the operator was paged only when escalation failed to
    deliver (#58). But an escalation happening *at all* means a customer has
    not answered a CRITICAL for the whole window — which is something Minas
    needs to know whether or not the message left the building."""
    account_id = _account(db)
    alert_id = _stale_critical(db, account_id)
    alerter = SpyAlerter()

    Escalator([Recording()], unacknowledged_minutes=15, operator_alerter=alerter).check(db)

    rules_paged = [c["rule"] for c in alerter.calls]
    assert "critical_alert_escalated" in rules_paged
    assert alerter.calls[0]["evidence"]["original_alert_id"] == alert_id


def test_a_delivered_escalation_pages_once_not_twice(db):
    """The #58 page fires only when nothing was delivered. A working
    escalation must produce the "this escalated" page and *not* the "reached
    nobody" one, or every escalation reads as a delivery failure."""
    account_id = _account(db)
    _stale_critical(db, account_id)
    alerter = SpyAlerter()

    Escalator([Recording()], unacknowledged_minutes=15, operator_alerter=alerter).check(db)

    assert [c["rule"] for c in alerter.calls] == ["critical_alert_escalated"]


def test_a_failed_escalation_pages_both(db):
    """And when it *does* fail, both facts are true and both are worth saying:
    a customer has not answered, and we could not tell them again."""
    account_id = _account(db)
    _stale_critical(db, account_id)
    alerter = SpyAlerter()

    class Broken:
        def send(self, alert):
            raise RuntimeError("down")

    Escalator([Broken()], unacknowledged_minutes=15, operator_alerter=alerter).check(db)

    assert set(c["rule"] for c in alerter.calls) == {
        "critical_alert_escalated", "escalation_delivered_to_nobody"}


def test_acknowledging_still_stops_everything(db):
    """The fourth criterion, unchanged but re-pinned now that there are more
    moving parts: an acknowledged alert produces no escalation, no contact
    delivery and no page."""
    account_id = _account(db, alert_email="primary@example.com")
    alert_id = _stale_critical(db, account_id)
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_alerts SET acknowledged_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc).isoformat(), alert_id))
    router, alerter, channel = SpyRouter(), SpyAlerter(), Recording()

    escalated = Escalator([channel], unacknowledged_minutes=15,
                          contact_router=router, operator_alerter=alerter).check(db)

    assert (escalated, router.calls, alerter.calls, channel.sent) == (0, [], [], [])


def test_the_escalator_is_constructed_with_both():
    """Both are optional parameters, which is exactly the shape that ends up
    never being passed.

    Matched against the `Escalator(...)` call specifically, not against the
    whole function. `contact_router` appears in `cmd_run` several times — the
    variable, the Engine argument — so a test looking for the bare name passes
    even when the escalator is built without one. That is the same
    name-versus-argument mistake this file's neighbours have already made
    twice."""
    import inspect
    import re

    from guardian import worker

    source = inspect.getsource(worker.main)
    call = re.search(r"Escalator\((?:[^()]|\([^()]*\))*\)", source)
    assert call, "no Escalator(...) construction found in worker.main"
    assert "contact_router=contact_router" in call.group(0), call.group(0)
    assert "operator_alerter=operator_alerter" in call.group(0), call.group(0)
