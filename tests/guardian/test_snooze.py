"""Silencing an alert you are already dealing with, and nothing else (G-26, #30).

A snooze is the only feature in this product whose *purpose* is to stop an
alert reaching someone. Everything else that suppresses — dedup, cooldown,
severity routing — suppresses a repeat of something already delivered. A snooze
suppresses a delivery outright, at the customer's request, and the request
arrives at the exact moment a customer is most likely to be wrong about what is
happening to their account.

So the tests are written around the ways a snooze could suppress more than the
customer asked for:

- a snooze is keyed on the *specific alert*, so a second, different occurrence
  of the same rule still fires — a withdrawal to a new address is not the
  withdrawal you were dealing with;
- a snooze never crosses to another rule, another account, or another
  customer;
- a snooze cannot be permanent, and cannot be made permanent by asking for a
  long enough duration;
- an expired snooze suppresses nothing, including for escalation;
- a snoozed alert is still detected, still recorded, and still visible — the
  customer silenced their phone, not the monitoring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from guardian.alerts.dispatch import _dedup_key, dispatch
from guardian.alerts.escalation import Escalator
from guardian.detectors.base import Detection
from guardian.snooze import MAX_SNOOZE_HOURS, active_snooze, snooze_alert
from tests.guardian.conftest import make_account

COOLDOWN = {"CRITICAL": 15, "HIGH": 30, "MEDIUM": 720, "LOW": 1440}


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _detection(rule="withdrawal_initiated", severity="CRITICAL", **evidence):
    return Detection(rule=rule, severity=severity, confidence="high",
                     message="0.5 BTC left the account", evidence=evidence or {"order_id": "wd-1"})


def _dispatch(db, channel, account_id, detection, label="acct-1"):
    return dispatch(db, {detection.severity: [channel]}, account_id, label, detection, COOLDOWN)


def _alerts(db, account_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT rule, delivered_to, snoozed_until FROM guardian_alerts WHERE account_id = %s "
            "ORDER BY id", (account_id,))
        return cur.fetchall()


# --- a snooze covers the alert you snoozed, not the rule ----------------------


def test_a_second_different_occurrence_of_the_same_rule_still_fires(db):
    """The failure that would make this feature dangerous.

    A customer snoozes "withdrawal to an unrecognised address" because they
    recognise *that* address. If the snooze covered the rule, the attacker's
    next withdrawal — to a different address, which is the whole reason the
    rule exists — arrives silently. Keying on the alert's own identity is what
    keeps the second one loud."""
    account_id = make_account(db)
    channel = RecordingChannel()
    first = _detection(order_id="wd-1", address="known-cold-wallet")
    _dispatch(db, channel, account_id, first)
    assert len(channel.sent) == 1

    snooze_alert(db, account_id, _dedup_key(account_id, first), first.rule,
                 hours=4, requested_by="alice@example.com")

    # Same rule, different withdrawal.
    _dispatch(db, channel, account_id, _detection(order_id="wd-2", address="attacker-wallet"))

    assert len(channel.sent) == 2
    assert channel.sent[1].evidence["address"] == "attacker-wallet"


def test_the_snoozed_alert_itself_is_silenced(db):
    """And the thing the customer actually asked for still works, so the test
    above is not passing because snoozing does nothing at all."""
    account_id = make_account(db)
    channel = RecordingChannel()
    detection = _detection(order_id="wd-1")
    _dispatch(db, channel, account_id, detection)

    snooze_alert(db, account_id, _dedup_key(account_id, detection), detection.rule,
                 hours=4, requested_by="alice@example.com")
    # Far enough past the CRITICAL cooldown that only the snooze can suppress it.
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_alerts SET sent_at = %s WHERE account_id = %s",
                    ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), account_id))

    _dispatch(db, channel, account_id, detection)

    assert len(channel.sent) == 1


def test_a_snooze_on_one_rule_never_silences_another(db):
    """The stated criterion. Two rules firing on the same underlying
    withdrawal share an account and an order id — everything except the rule
    name — so this is where a key built from the wrong fields shows up."""
    account_id = make_account(db)
    channel = RecordingChannel()
    withdrawal = _detection(rule="withdrawal_initiated", order_id="wd-1")
    unknown_address = _detection(rule="withdrawal_to_unknown_address", order_id="wd-1")

    snooze_alert(db, account_id, _dedup_key(account_id, withdrawal), withdrawal.rule,
                 hours=4, requested_by="alice@example.com")
    _dispatch(db, channel, account_id, unknown_address)

    assert [a.rule for a in channel.sent] == ["withdrawal_to_unknown_address"]


def test_a_snooze_never_crosses_to_another_account(db):
    """Two accounts belonging to the same customer produce identical
    detections all the time — the same coin, the same exchange, often the same
    order ids from a mirrored setup. The account has to be part of the key."""
    alice = make_account(db, label="alice-1")
    bob = make_account(db, label="bob-1")
    channel = RecordingChannel()
    detection = _detection(order_id="wd-1")

    snooze_alert(db, alice, _dedup_key(alice, detection), detection.rule,
                 hours=4, requested_by="alice@example.com")
    _dispatch(db, channel, bob, detection, label="bob-1")

    assert len(channel.sent) == 1


# --- a snooze always expires --------------------------------------------------


def test_an_expired_snooze_suppresses_nothing(db):
    account_id = make_account(db)
    channel = RecordingChannel()
    detection = _detection(order_id="wd-1")
    key = _dedup_key(account_id, detection)
    snooze_alert(db, account_id, key, detection.rule, hours=4, requested_by="alice@example.com")
    with db.cursor() as cur:  # wind it past its expiry
        cur.execute("UPDATE guardian_snoozes SET expires_at = %s WHERE dedup_key = %s",
                    ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), key))

    assert active_snooze(db, account_id, key) is None
    _dispatch(db, channel, account_id, detection)
    assert len(channel.sent) == 1


def test_a_snooze_cannot_be_asked_for_indefinitely(db):
    """"Snoozes always expire — there is no permanent mute."

    A cap that only lives in the dropdown is not a cap: the form is a POST
    anyone can retype. The clamp is in the function that writes the row, and
    the column is NOT NULL, so the table cannot represent a snooze that never
    ends even if every caller is wrong."""
    account_id = make_account(db)
    before = datetime.now(timezone.utc)

    row = snooze_alert(db, account_id, "k", "withdrawal_initiated",
                       hours=24 * 3650, requested_by="alice@example.com")

    expires = datetime.fromisoformat(row["expires_at"])
    assert expires <= before + timedelta(hours=MAX_SNOOZE_HOURS) + timedelta(seconds=5)


@pytest.mark.parametrize("hours", [0, -1, -10_000])
def test_a_zero_or_negative_duration_is_not_a_snooze(db, hours):
    """`hours=0` reads as "snooze for no time", which is a no-op — but through
    a naive `now + timedelta(hours=hours)` it writes a row that is already
    expired, and through a negative one it writes a snooze that expired in the
    past and would look like a mute if any comparison were ever flipped.
    Neither is a thing a customer can ask for."""
    account_id = make_account(db)

    assert snooze_alert(db, account_id, "k", "withdrawal_initiated",
                        hours=hours, requested_by="alice@example.com") is None
    assert active_snooze(db, account_id, "k") is None


# --- a snooze silences the phone, not the monitoring --------------------------


def test_a_snoozed_alert_is_still_detected_and_recorded(db):
    """"Snoozed alerts stay visible in the portal."

    The alert row is written whether or not anyone was told — the same rule
    #57 established for a failed delivery. A snooze that dropped the detection
    would leave nothing in the history a customer could later hand to an
    exchange, and would make "I silenced it" indistinguishable from "it never
    happened"."""
    account_id = make_account(db)
    channel = RecordingChannel()
    detection = _detection(order_id="wd-1")
    snooze_alert(db, account_id, _dedup_key(account_id, detection), detection.rule,
                 hours=4, requested_by="alice@example.com")

    _dispatch(db, channel, account_id, detection)

    rows = _alerts(db, account_id)
    assert len(rows) == 1
    assert rows[0]["rule"] == "withdrawal_initiated"
    assert channel.sent == []


def test_a_snoozed_alert_is_marked_as_snoozed_not_as_undelivered(db):
    """`delivered_to = 0` already means "we tried to tell someone and reached
    nobody" (#57) — an operator-visible fault. A snooze is the opposite: it
    worked exactly as asked. Sharing one column would turn every snooze into a
    false report of a broken alert channel."""
    account_id = make_account(db)
    detection = _detection(order_id="wd-1")
    snooze_alert(db, account_id, _dedup_key(account_id, detection), detection.rule,
                 hours=4, requested_by="alice@example.com")

    _dispatch(db, RecordingChannel(), account_id, detection)

    row = _alerts(db, account_id)[0]
    assert row["snoozed_until"] is not None
    assert row["delivered_to"] == 0


def test_an_unsnoozed_alert_carries_no_snooze_marker(db):
    """So the marker above means something."""
    account_id = make_account(db)
    _dispatch(db, RecordingChannel(), account_id, _detection(order_id="wd-1"))

    assert _alerts(db, account_id)[0]["snoozed_until"] is None


# --- escalation ----------------------------------------------------------------


def test_escalation_holds_while_the_snooze_is_live_and_resumes_after(db):
    """A snooze that escalation ignored would page the customer anyway and the
    button would be a lie. A snooze that suppressed escalation *permanently*
    would be the permanent mute this feature is not allowed to have. So it
    holds, and then it lets go — which is the whole reason snoozes expire."""
    account_id = make_account(db)
    detection = _detection(order_id="wd-1")
    key = _dedup_key(account_id, detection)
    snooze_alert(db, account_id, key, detection.rule, hours=4, requested_by="alice@example.com")
    _dispatch(db, RecordingChannel(), account_id, detection)
    with db.cursor() as cur:  # old enough to escalate
        cur.execute("UPDATE guardian_alerts SET sent_at = %s WHERE account_id = %s",
                    ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(), account_id))

    assert Escalator([], unacknowledged_minutes=15).check(db) == 0

    with db.cursor() as cur:
        cur.execute("UPDATE guardian_snoozes SET expires_at = %s WHERE dedup_key = %s",
                    ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), key))

    assert Escalator([], unacknowledged_minutes=15).check(db) == 1


# --- the record of who asked --------------------------------------------------


def test_a_snooze_records_who_asked_for_it_and_until_when(db):
    """Someone silenced an alarm on an account that may later be the subject of
    an exchange dispute. That is a fact about a person and a time, and it has
    to survive the snooze expiring."""
    account_id = make_account(db)

    row = snooze_alert(db, account_id, "k", "withdrawal_initiated",
                       hours=4, requested_by="alice@example.com")

    assert row["requested_by"] == "alice@example.com"
    assert row["rule"] == "withdrawal_initiated"
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
    assert datetime.fromisoformat(row["created_at"]) <= datetime.now(timezone.utc)


# --- the cap depends on how bad the thing is (decision 8) ----------------------


@pytest.mark.parametrize("severity,cap", [
    ("CRITICAL", 1), ("HIGH", 4), ("MEDIUM", 24), ("LOW", 24),
])
def test_the_cap_is_per_severity(db, severity, cap):
    """Acknowledge already stops escalation and says "I have seen this". A
    snooze only suppresses repeats, and the only legitimate reason to suppress
    repeats of a CRITICAL is that you are dealing with it *right now* — which
    is a short window by definition.

    A drain unfolds over minutes to hours. At 24 hours a CRITICAL snooze is
    long enough for the rest of one to finish in silence. Re-snoozing costs one
    tap and forces a fresh decision, which is the right friction to put in
    front of continuing to ignore a CRITICAL."""
    account_id = make_account(db)
    before = datetime.now(timezone.utc)

    row = snooze_alert(db, account_id, "k", "withdrawal_initiated",
                       hours=24 * 3650, requested_by="alice@example.com", severity=severity)

    expires = datetime.fromisoformat(row["expires_at"])
    assert expires <= before + timedelta(hours=cap) + timedelta(seconds=5)
    # And the cap is not silently the same for everything: a CRITICAL snooze
    # must be strictly shorter than a MEDIUM one.
    if severity == "CRITICAL":
        assert expires < before + timedelta(hours=2)


def test_an_unknown_severity_gets_the_shortest_cap(db):
    """A severity this code does not recognise is not a reason to grant the
    longest silence available. Failing towards *less* suppression is the only
    safe direction for a feature whose purpose is suppression."""
    account_id = make_account(db)
    before = datetime.now(timezone.utc)

    row = snooze_alert(db, account_id, "k", "some_rule",
                       hours=24, requested_by="alice@example.com", severity="WEIRD")

    assert datetime.fromisoformat(row["expires_at"]) <= before + timedelta(hours=1, seconds=5)


def test_a_duration_within_the_cap_is_honoured_exactly(db):
    """The cap is a ceiling, not a setting. Asking for one hour on a MEDIUM
    gives one hour, not 24."""
    account_id = make_account(db)
    before = datetime.now(timezone.utc)

    row = snooze_alert(db, account_id, "k", "abnormal_fee_spend",
                       hours=1, requested_by="alice@example.com", severity="MEDIUM")

    expires = datetime.fromisoformat(row["expires_at"])
    assert timedelta(minutes=59) <= expires - before <= timedelta(hours=1, seconds=5)


def test_the_portal_offers_no_duration_beyond_the_severitys_cap(db):
    """The dropdown is not the enforcement — `snooze_alert` is — but offering a
    24h option on a CRITICAL that will be silently clamped to 1h tells the
    customer something untrue about what they just did."""
    from guardian.snooze import snooze_choices

    assert max(snooze_choices("CRITICAL")) <= 1
    assert max(snooze_choices("HIGH")) <= 4
    assert max(snooze_choices("MEDIUM")) <= 24
    assert snooze_choices("WEIRD") == snooze_choices("CRITICAL")


class _Caps:
    """Minimal stand-in for RulesConfig.get, so these tests exercise the real
    lookup rather than a mock that agrees with whatever the code does."""

    def __init__(self, table):
        self._table = table

    def get(self, *keys, default=None):
        if keys[:2] == ("alerting", "snooze_cap_hours"):
            return self._table.get(keys[2], default)
        return default


def test_a_configured_cap_of_zero_means_that_severity_cannot_be_snoozed(db):
    """A cap of 0 is decision 8's option 3 — "CRITICAL cannot be snoozed at
    all" — and it is a legitimate thing for an owner to set.

    It is also a falsy value standing for a real decision, which is the bug
    pattern this codebase has shipped five times. Read with `if configured:`
    the deliberate zero looks like "not set" and the built-in one-hour cap
    comes back, silently re-enabling the suppression the owner turned off. The
    lookup uses `is not None` for exactly that reason."""
    from guardian.snooze import cap_hours

    account_id = make_account(db)
    rules = _Caps({"CRITICAL": 0})

    assert cap_hours("CRITICAL", rules) == 0
    assert snooze_alert(db, account_id, "k", "r", hours=4, requested_by="a@b.c",
                        severity="CRITICAL", rules=rules) is None
    assert active_snooze(db, account_id, "k") is None
    # HIGH is unaffected: a zero on one severity is not a zero on all of them.
    assert snooze_alert(db, account_id, "k2", "r", hours=4, requested_by="a@b.c",
                        severity="HIGH", rules=rules) is not None


def test_a_severity_that_cannot_be_snoozed_offers_no_button(db):
    """With a zero cap there is nothing to offer, and the fallback that keeps
    an unusually small cap selectable must not turn that into a "0h" button —
    a control that submits and is then refused is worse than no control."""
    from guardian.snooze import snooze_choices

    assert snooze_choices("CRITICAL", _Caps({"CRITICAL": 0})) == ()
    # The fallback still works for a small but real cap.
    assert snooze_choices("CRITICAL", _Caps({"CRITICAL": 0.5})) == (0.5,)


def test_the_configured_cap_beats_the_built_in_table(db):
    """The caps live in `alerting.snooze_cap_hours` so they are tunable without
    a deploy (G-12) and every change to them is recorded against a name (#16).
    A config value the code ignores is the same defect as no config."""
    from guardian.snooze import cap_hours

    assert cap_hours("CRITICAL") == 1
    assert cap_hours("CRITICAL", _Caps({"CRITICAL": 2})) == 2
    # An unconfigured severity still falls back to the table, not to zero.
    assert cap_hours("HIGH", _Caps({"CRITICAL": 2})) == 4


