"""#59: "last polled successfully" must mean the account was examined.

`last_success_at` was stamped immediately after the exchange fetches returned,
with nine detector invocations still to run - and `consecutive_failures` was
reset to 0 at the same moment. So a cycle that died partway through:

  - counted as a success,
  - never incremented the failure counter, so `detect_unreachable` never fired,
  - showed green on the dashboard with a `last_success_at` of seconds ago,
  - and produced one log line about a "task failure" from the worker's blanket
    handler, because the exception escapes `run_fast_loop` entirely.

An account could sit in that state indefinitely: polled every 45 seconds, green
on every indicator, never actually checked.

Found via G-12, where a mistyped threshold raised from inside the detector
section. That trigger is fixed; the trigger was never the problem.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from guardian.config import RulesConfig
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller, _get_poll_state
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import SHIPPED_RULES


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)

    @property
    def rules(self):
        return {a.rule for a in self.sent}


def _client():
    c = MagicMock()
    c.fetch_deposits.return_value = []
    c.fetch_withdrawals.return_value = []
    c.fetch_closed_orders.return_value = []
    c.fetch_balance.return_value = {"total": {"BTC": 1.0}}
    c.fetch_account_info.return_value = account_info_response()
    c.fetch_tickers.return_value = {}
    c.fetch_transfers.return_value = []
    return c


@pytest.fixture
def account(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="honest", topology="independent",
                             api_key="k", secret="s", passphrase="p")
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _client())
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        return result.account_id, cur.fetchone()


def _poller(db, cipher, channel=None):
    channel = channel or Recording()
    return AccountPoller(
        db, cipher, RulesConfig(SHIPPED_RULES),
        {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]},
    ), channel


def _break_a_detector(monkeypatch):
    """Fails one of the nine detectors that run after the fetches. Which one
    doesn't matter - the point is that *any* of them can, and the trigger that
    exposed this (a bad config value) is only one of many."""
    def boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr("guardian.poller.detect_new_withdrawals", boom)


# --- a dead cycle is not a success -------------------------------------------

def test_a_crash_after_the_fetches_does_not_record_a_success(db, cipher, monkeypatch, account):
    account_id, row = account
    poller, _ = _poller(db, cipher)
    poller.run_fast_loop(row)  # one honest success first
    before = _get_poll_state(db, account_id)["last_success_at"]

    _break_a_detector(monkeypatch)
    poller.run_fast_loop(row)

    after = _get_poll_state(db, account_id)["last_success_at"]
    assert after == before, (
        "a cycle that died after the fetches was recorded as a successful poll"
    )


def test_a_crash_after_the_fetches_counts_as_a_failure(db, cipher, monkeypatch, account):
    """The counter was *reset* before the detectors ran, so a cycle dying every
    time still reported zero consecutive failures forever."""
    account_id, row = account
    poller, _ = _poller(db, cipher)
    _break_a_detector(monkeypatch)

    poller.run_fast_loop(row)

    assert _get_poll_state(db, account_id)["consecutive_failures"] == 1


def test_repeated_crashes_eventually_alert_the_customer(db, cipher, monkeypatch, account):
    """This is G-10's guarantee - *losing sight of an account is itself an
    alert* - and it did not hold. The shipped heartbeat threshold is 5."""
    account_id, row = account
    poller, channel = _poller(db, cipher)
    _break_a_detector(monkeypatch)

    for _ in range(5):
        poller.run_fast_loop(row)

    assert "account_unreachable" in channel.rules, (
        "an account whose cycles keep dying was never reported as unreachable"
    )


# --- and a real success still is one -----------------------------------------

def test_a_completed_cycle_records_a_success(db, cipher, account):
    account_id, row = account
    poller, _ = _poller(db, cipher)

    poller.run_fast_loop(row)

    state = _get_poll_state(db, account_id)
    assert state["last_success_at"] is not None
    assert state["consecutive_failures"] == 0


def test_a_success_clears_earlier_failures(db, cipher, monkeypatch, account):
    """Recovery has to work, or one bad cycle marks an account failing forever."""
    account_id, row = account
    poller, _ = _poller(db, cipher)

    _break_a_detector(monkeypatch)
    poller.run_fast_loop(row)
    assert _get_poll_state(db, account_id)["consecutive_failures"] == 1

    monkeypatch.undo()
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _client())
    poller.run_fast_loop(row)

    state = _get_poll_state(db, account_id)
    assert state["consecutive_failures"] == 0
    assert state["last_success_at"] is not None


def test_the_fill_window_does_not_advance_past_a_dead_cycle(db, cipher, monkeypatch, account):
    """`last_fast_loop_at` bounds which fills are still worth price-checking
    against the current ticker. Advancing it for a cycle that never reached the
    fill section would step the window over fills nothing ever judged."""
    account_id, row = account
    poller, _ = _poller(db, cipher)
    poller.run_fast_loop(row)
    before = _get_poll_state(db, account_id)["last_fast_loop_at"]

    _break_a_detector(monkeypatch)
    poller.run_fast_loop(row)

    assert _get_poll_state(db, account_id)["last_fast_loop_at"] == before
