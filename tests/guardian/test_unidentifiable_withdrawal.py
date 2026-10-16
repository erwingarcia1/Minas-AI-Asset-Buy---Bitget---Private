"""A withdrawal we cannot tell apart from another one (#67).

`withdrawal_identity` digests coin, amount, address, tag, network, type and
timestamp when the exchange gives no `id` and no `txid`. With the timestamp
also missing, two genuinely separate withdrawals of the same amount to the same
address share one identity — so `record_seen_withdrawal` reports the second as
already seen and it never reaches a detector.

The collision itself is not fixed here. Options 2 and 3 in #67 both trade
against the property `withdrawal_identity` exists to hold — that one settling
withdrawal keeps one identity and does not page the customer on every poll —
and neither should be chosen without knowing how often this actually happens.

What is fixed here is the *silence*. Before this, the second withdrawal was
swallowed and the system reported a clean poll: no log line, no metric, no
page. A documented limitation is not a notification, and the docstring saying
an operator "should be looking at this directly" did not give them anything to
look at.

So: page the operator, count it, and leave the collision visible and measured
until there is data to choose a fix from.
"""
from __future__ import annotations

import pytest

from guardian.identity import is_unidentifiable, withdrawal_identity


def _record(**overrides):
    record = {"currency": "BTC", "amount": "0.5", "address": "bc1qattacker",
              "network": "BTC", "type": "withdrawal"}
    record.update(overrides)
    return record


# --- recognising the case -----------------------------------------------------


def test_a_withdrawal_with_neither_a_reference_nor_a_time_is_unidentifiable():
    assert is_unidentifiable(_record()) is True


@pytest.mark.parametrize("field,value", [
    ("id", "wd-1"),
    ("txid", "0xdeadbeef"),
    ("timestamp", 1_700_000_000_000),
])
def test_any_one_of_a_reference_or_a_time_is_enough(field, value):
    """Only the *combination* is dangerous. An id alone identifies the record
    outright; a timestamp alone makes the digest distinguish two withdrawals
    that are otherwise identical, because two of them cannot share a
    millisecond."""
    assert is_unidentifiable(_record(**{field: value})) is False


@pytest.mark.parametrize("empty", ["", None, 0])
def test_an_empty_reference_counts_as_no_reference(empty):
    """`id: ""` and `id: 0` are what an exchange sends when it has no reference
    but its schema requires the field. Treating a falsy id as an id is the
    falsy-sentinel pattern, and here it would mean the page never fires for the
    records most likely to need it — `withdrawal_identity` already discards
    these, so anything that disagrees with it is wrong."""
    record = _record(id=empty, txid=empty, timestamp=empty)

    assert is_unidentifiable(record) is True
    assert withdrawal_identity(record).startswith("sha256:")


def test_a_zero_timestamp_is_not_a_time():
    """Unix epoch is not a plausible withdrawal time, and 0 is what a missing
    integer field becomes on the way through a badly-behaved adapter. Same
    lesson as `monotonic()`: 0 is not "no value"."""
    assert is_unidentifiable(_record(timestamp=0)) is True


# --- the operator is told -----------------------------------------------------


def test_the_poller_pages_the_operator_and_counts_it(db, rules, monkeypatch):
    from tests.guardian.helpers.poller_harness import poll_once_with_withdrawals

    pages, metrics = poll_once_with_withdrawals(db, rules, [_record()])

    assert [p["rule"] for p in pages] == ["withdrawal_cannot_be_identified"]
    assert metrics["unidentifiable_withdrawals"] == 1


def test_an_identifiable_withdrawal_pages_nobody(db, rules):
    """So the page above means something. Ordinary withdrawals are the
    overwhelming majority, and a page on every one of them is a page on none
    of them."""
    from tests.guardian.helpers.poller_harness import poll_once_with_withdrawals

    pages, metrics = poll_once_with_withdrawals(db, rules, [_record(id="wd-1")])

    assert pages == []
    assert metrics["unidentifiable_withdrawals"] == 0


def test_the_page_names_the_account_and_says_what_was_lost(db, rules):
    """An operator reading this at 3am needs to know which customer, and that
    the consequence is a *missed alert* rather than a formatting complaint."""
    from tests.guardian.helpers.poller_harness import poll_once_with_withdrawals

    pages, _ = poll_once_with_withdrawals(db, rules, [_record(amount="0.5")])

    page = pages[0]
    assert page["severity"] == "HIGH"
    assert "acct-1" in page["message"]
    assert "0.5" in page["message"] or "0.5" in str(page["evidence"])


def test_the_withdrawal_is_still_detected(db, rules):
    """The page is added to the existing behaviour, not substituted for it.
    An unidentifiable withdrawal is still a withdrawal, and the customer still
    gets their CRITICAL — it is the *second* one that is lost, which is exactly
    why the operator needs to know."""
    from tests.guardian.helpers.poller_harness import poll_once_with_withdrawals

    pages, _ = poll_once_with_withdrawals(db, rules, [_record()])

    with db.cursor() as cur:
        cur.execute("SELECT rule FROM guardian_alerts ORDER BY id")
        rules_fired = [r["rule"] for r in cur.fetchall()]
    assert "withdrawal_initiated" in rules_fired
    assert len(pages) == 1


def test_two_colliding_withdrawals_page_once_not_twice(db, rules):
    """The page is about the *condition*, not about each record. An exchange
    that has stopped sending references sends a poll full of them, and one
    page per record would bury the operator in the thing they are being told
    about."""
    from tests.guardian.helpers.poller_harness import poll_once_with_withdrawals

    pages, metrics = poll_once_with_withdrawals(db, rules, [_record(), _record(), _record()])

    assert len(pages) == 1
    # The metric still counts every record, so the real frequency is knowable
    # even though the paging is deduped.
    assert metrics["unidentifiable_withdrawals"] == 3
