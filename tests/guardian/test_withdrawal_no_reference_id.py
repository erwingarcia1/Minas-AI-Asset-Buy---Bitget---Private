"""G-11 (#15): a withdrawal the exchange reports without a reference ID must
still be alerted.

Two separate places dropped it, and fixing either alone leaves the customer
with a withdrawal nobody told them about:

1. `record_seen_withdrawal` returned False for a record with no `id`/`txid`.
   The poller reads that as "already seen", so the record never reached
   `detect_new_withdrawals` or `detect_unseen_address` at all.

2. `dispatch._dedup_key` falls back to `coin` when evidence carries no
   `order_id`. Two distinct id-less withdrawals of the same coin therefore
   share one dedup key, and the second is suppressed as a repeat of the first
   for the whole CRITICAL cooldown.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from guardian.config import RulesConfig
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller
from tests.guardian.fixtures.ccxt_responses import account_info_response, withdrawal_record
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import SHIPPED_RULES


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _client(withdrawals=None, balance_total=None):
    client = MagicMock()
    client.fetch_deposits.return_value = []
    client.fetch_withdrawals.return_value = withdrawals or []
    client.fetch_closed_orders.return_value = []
    client.fetch_balance.return_value = {"total": balance_total or {"BTC": 1.0}}
    client.fetch_account_info.return_value = account_info_response()
    client.fetch_tickers.return_value = {}
    client.fetch_transfers.return_value = []
    return client


def _unidentified(amount="0.5", address="bc1qexistingaddress", status="pending", timestamp=1700000000000):
    """A withdrawal exactly as the exchange returned it, with no reference ID
    of any kind - neither `id` nor `txid`."""
    record = withdrawal_record(amount=amount, address=address, status=status, timestamp=timestamp)
    record["id"] = None
    record.pop("txid", None)
    return record


def _poller(db, cipher, channel):
    return AccountPoller(
        db,
        cipher,
        RulesConfig(SHIPPED_RULES),
        {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]},
    )


def _account(db, cipher, monkeypatch, label):
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label=label, topology="independent", api_key="k", secret="s", passphrase="p"
    )
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        return result.account_id, cur.fetchone()


def test_withdrawal_without_a_reference_id_still_alerts(db, cipher, monkeypatch):
    """AC: withdrawals the exchange reports without a reference ID are still
    alerted. This is the whole issue - money left the account and nobody was
    told."""
    account_id, account_row = _account(db, cipher, monkeypatch, "noid-1")

    monkeypatch.setattr(
        "guardian.poller.make_client", lambda *a, **k: _client(withdrawals=[_unidentified()])
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    assert "withdrawal_initiated" in {a.rule for a in channel.sent}


def test_id_less_withdrawal_to_an_unseen_address_is_still_flagged(db, cipher, monkeypatch):
    """AC: withdrawals to a destination never used before are marked as higher
    risk. The unseen-address detector reads the same filtered list, so it was
    blind to id-less withdrawals for the same reason."""
    account_id, account_row = _account(db, cipher, monkeypatch, "noid-2")

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(withdrawals=[_unidentified(address="bc1qneverseenbefore")]),
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    fired = {a.rule for a in channel.sent}
    assert "withdrawal_to_unseen_address" in fired
    assert [a.severity for a in channel.sent if a.rule == "withdrawal_to_unseen_address"] == ["CRITICAL"]


def test_the_same_id_less_withdrawal_does_not_refire(db, cipher, monkeypatch):
    """It has no ID, so "have I seen this one?" has to be answered some other
    way - but it still has to be answered. The exchange returns the same
    record on every poll."""
    account_id, account_row = _account(db, cipher, monkeypatch, "noid-3")

    monkeypatch.setattr(
        "guardian.poller.make_client", lambda *a, **k: _client(withdrawals=[_unidentified()])
    )
    channel = RecordingChannel()
    poller = _poller(db, cipher, channel)
    poller.run_fast_loop(account_row)
    poller.run_fast_loop(account_row)
    poller.run_fast_loop(account_row)

    initiated = [a for a in channel.sent if a.rule == "withdrawal_initiated"]
    assert len(initiated) == 1, f"fired {len(initiated)} times, expected once"


def test_status_change_on_an_id_less_withdrawal_does_not_refire(db, cipher, monkeypatch):
    """A withdrawal record mutates as it settles: pending -> success, and a
    txid can appear later. Identity must be built from the fields that don't
    move, or every state transition reads as a brand new withdrawal and the
    customer is paged three times for one movement.

    This is the guard against the tempting wrong fix of digesting the whole
    record the way an immutable fill can be."""
    account_id, account_row = _account(db, cipher, monkeypatch, "noid-4")

    channel = RecordingChannel()
    poller = _poller(db, cipher, channel)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(withdrawals=[_unidentified(status="pending")]),
    )
    poller.run_fast_loop(account_row)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(withdrawals=[_unidentified(status="success")]),
    )
    poller.run_fast_loop(account_row)

    initiated = [a for a in channel.sent if a.rule == "withdrawal_initiated"]
    assert len(initiated) == 1, f"a status transition re-fired the alert ({len(initiated)} times)"


def test_two_distinct_id_less_withdrawals_both_alert(db, cipher, monkeypatch):
    """The dedup layer. Both have no `order_id` in evidence, so
    `dispatch._dedup_key` falls back to the coin - identical for both - and the
    second is silently suppressed as a repeat of the first.

    Two withdrawals left the account. The customer must hear about two."""
    account_id, account_row = _account(db, cipher, monkeypatch, "noid-5")

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(
            withdrawals=[
                _unidentified(amount="0.5", address="bc1qfirstdestination", timestamp=1700000000000),
                _unidentified(amount="0.9", address="bc1qseconddestination", timestamp=1700000500000),
            ]
        ),
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    initiated = [a for a in channel.sent if a.rule == "withdrawal_initiated"]
    assert len(initiated) == 2, (
        f"only {len(initiated)} of 2 distinct withdrawals were alerted - the other was deduped away"
    )


def test_identified_withdrawals_are_unaffected(db, cipher, monkeypatch):
    """The path that already worked keeps working, and keeps reporting the
    exchange's own reference in the evidence an operator reads."""
    account_id, account_row = _account(db, cipher, monkeypatch, "noid-6")

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(
            withdrawals=[withdrawal_record(order_id="wd-real", address="bc1qneveraddress")]
        ),
    )
    channel = RecordingChannel()
    poller = _poller(db, cipher, channel)
    poller.run_fast_loop(account_row)
    poller.run_fast_loop(account_row)

    initiated = [a for a in channel.sent if a.rule == "withdrawal_initiated"]
    assert len(initiated) == 1
    assert initiated[0].evidence["order_id"] == "wd-real"
