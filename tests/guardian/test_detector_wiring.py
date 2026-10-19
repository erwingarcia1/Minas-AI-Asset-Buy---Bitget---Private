"""Every detector is actually called by the poller.

A mutation sweep - blanking each `for detection in detect_x(...)` call site in
turn and running the whole suite - found that **8 of 12 detectors were
unpinned**. Their detection logic was thoroughly unit-tested, and nothing at all
failed when the poller stopped calling them.

That is the same fault as the two found earlier in `_check_config_health` and
the untimestamped-record alarm, and it is the fault this codebase keeps
producing: testing what a component decides, and never testing that anything
asks it. A detector nobody calls is indistinguishable from a detector that never
fires, and the customer sees the same thing either way - a quiet dashboard.

These tests deliberately assert almost nothing about *how* each detector
decides. That belongs in the per-detector unit tests and is well covered there.
Each one here asks a single question: drive the poller with input this detector
should fire on, and does its rule come out?
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from guardian.config import RulesConfig
from guardian.exchange import TamperSignal
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller
from tests.guardian.fixtures.ccxt_responses import account_info_response, withdrawal_record
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


def _client(deposits=None, withdrawals=None, fills=None, balance=None,
            authorities="stor", transfers=None, tickers=None):
    c = MagicMock()
    c.fetch_deposits.return_value = deposits or []
    c.fetch_withdrawals.return_value = withdrawals or []
    c.fetch_closed_orders.return_value = fills or []
    c.fetch_balance.return_value = {"total": balance if balance is not None else {"BTC": 1.0}}
    c.fetch_account_info.return_value = account_info_response(authorities=authorities)
    c.fetch_tickers.return_value = tickers or {}
    c.fetch_transfers.return_value = transfers or []
    return c


@pytest.fixture
def account(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="wiring", topology="independent", api_key="k", secret="s", passphrase="p"
    )
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        return result.account_id, cur.fetchone()


def _poller(db, cipher, channel):
    return AccountPoller(
        db, cipher, RulesConfig(SHIPPED_RULES),
        {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]},
    )


def _fill(symbol="BTC/USDT", fee_cost="1", timestamp=1_900_000_000_000, price="50000"):
    return {
        "id": f"f-{symbol}-{fee_cost}-{timestamp}", "symbol": symbol, "side": "sell",
        "average": price, "price": price, "amount": "0.1", "filled": "0.1", "cost": "5000",
        "fee": {"currency": "USDT", "cost": fee_cost}, "timestamp": timestamp, "info": {},
    }


# --- fast loop ----------------------------------------------------------------

def test_withdrawal_burst_is_wired(db, cipher, monkeypatch, account):
    """Three withdrawals in one window. Shipped min_count is 3."""
    _, row = account
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _client(withdrawals=[
        withdrawal_record(order_id=f"burst-{i}", address="bc1qexistingaddress") for i in range(3)
    ]))
    channel = Recording()
    _poller(db, cipher, channel).run_fast_loop(row)

    assert "withdrawal_burst" in channel.rules


def test_baseline_drift_is_wired(db, cipher, monkeypatch, account):
    """A deposit from an address the account has never received from."""
    _, row = account
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _client(deposits=[
        {"id": "dep-new", "amount": "1", "currency": "BTC", "status": "ok",
         "address": "bc1qbrandnewsource", "timestamp": 1_900_000_000_000, "info": {}}
    ]))
    channel = Recording()
    _poller(db, cipher, channel).run_fast_loop(row)

    assert "baseline_drift_new_deposit_source" in channel.rules


def test_new_pair_activity_is_wired(db, cipher, monkeypatch, account):
    """A fill on a symbol the account has never traded."""
    _, row = account
    monkeypatch.setattr(
        "guardian.poller.make_client", lambda *a, **k: _client(fills=[_fill(symbol="SHIB/USDT")])
    )
    channel = Recording()
    _poller(db, cipher, channel).run_fast_loop(row)

    assert "never_touched_pair_activity" in channel.rules


def test_fee_anomaly_is_wired(db, cipher, monkeypatch, account):
    """Needs a baseline first, so this is two cycles: one to seed the EMA at a
    normal fee, then one that spends far above it."""
    _, row = account
    channel = Recording()
    poller = _poller(db, cipher, channel)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(fee_cost="2", timestamp=1_900_000_000_000)]),
    )
    poller.run_fast_loop(row)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(fee_cost="500", timestamp=1_900_000_100_000)]),
    )
    poller.run_fast_loop(row)

    assert "abnormal_fee_spend" in channel.rules


def test_new_transfer_route_is_wired(db, cipher, monkeypatch, account):
    """An internal transfer over a route the account has never used."""
    _, row = account
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _client(
        balance={"USDT": 500.0},
        transfers=[{"id": "tr-1", "fromAccount": "spot", "toAccount": "swap",
                    "currency": "USDT", "amount": 100, "timestamp": 1_900_000_000_000}],
    ))
    channel = Recording()
    _poller(db, cipher, channel).run_fast_loop(row)

    assert "transfer_route_never_seen" in channel.rules


def test_tamper_is_wired(db, cipher, monkeypatch, account):
    """Shipped threshold is 2 consecutive auth failures."""
    _, row = account
    channel = Recording()
    poller = _poller(db, cipher, channel)

    def refuses(*a, **k):
        raise TamperSignal("key rejected")

    monkeypatch.setattr("guardian.poller.make_client", refuses)
    poller.run_fast_loop(row)
    poller.run_fast_loop(row)

    assert "monitoring_key_tamper_suspected" in channel.rules


def test_unreachable_is_wired(db, cipher, monkeypatch, account):
    """Shipped threshold is 5 consecutive generic failures."""
    _, row = account
    channel = Recording()
    poller = _poller(db, cipher, channel)

    def broken(*a, **k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr("guardian.poller.make_client", broken)
    for _ in range(5):
        poller.run_fast_loop(row)

    assert "account_unreachable" in channel.rules


# --- slow loop ----------------------------------------------------------------

def test_permission_state_change_is_wired(db, cipher, monkeypatch, account):
    """The slow loop's job: the key's authorities changed since the last
    snapshot. Onboarding seeds the first snapshot, so one changed poll fires."""
    _, row = account
    channel = Recording()
    poller = _poller(db, cipher, channel)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(authorities="stor,withdraw"),
    )
    poller.run_slow_loop(row)

    assert "account_permission_state_changed" in channel.rules


# --- threads that must not die -----------------------------------------------

def test_the_scheduler_survives_a_failure_reading_the_poll_interval(db, cipher):
    """The interval read goes through RulesConfig, which touches the filesystem
    and re-parses YAML - so it can fail. Outside a handler it would kill the
    scheduler thread, and then nothing is ever queued again while the process
    stays up, the dashboard keeps serving and the metrics endpoint keeps
    answering. Total silence presented as health."""
    from guardian.engine import Engine

    engine = Engine(db, cipher, RulesConfig(SHIPPED_RULES), {})

    class Hostile:
        is_valid = True
        rejected_at = None

        def get_seconds(self, *a, **k):
            raise RuntimeError("config read exploded")

        def get_int(self, *a, **k):
            return 8

        def get(self, *a, **k):
            return None

    engine._rules = Hostile()
    delay = engine._scheduler_pass()  # must not raise

    assert delay >= 1.0, "the scheduler must still schedule its next pass"


def test_a_worker_thread_survives_a_failure_in_its_own_error_handling(db, cipher, monkeypatch):
    """The breaker update runs *because* something already went wrong. A failure
    there escapes the handler that exists to stop one bad account killing a
    worker - and a pool that loses workers one at a time ends up polling
    nothing, with no single moment where anything looks broken.

    Driven through the real `_worker_loop` on a real thread, because "the thread
    is still alive afterwards" is the actual guarantee and the synchronous
    drain used elsewhere does not have it."""
    import threading

    from guardian.engine import Engine, Task

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, exchange, label, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, status, onboarded_at, updated_at) "
            "VALUES ('00000000-0000-4000-8000-00000000a0a0', 'self', 'bitget', 'worker-death', 'independent', 'x', 'y', 'z', 'active', '2026-01-01', '2026-01-01')"
        )
        account_id = cur.lastrowid
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))

    engine = Engine(db, cipher, RulesConfig(SHIPPED_RULES), {})

    class ExplodingBreakers:
        """Fails everywhere the worker touches it, including inside the
        worker's own except-block."""

        def for_account(self, _):
            raise RuntimeError("breaker registry exploded")

    engine.breakers = ExplodingBreakers()

    survived = threading.Event()
    original_release = engine._release

    def release(account_id_, kind):
        original_release(account_id_, kind)
        survived.set()

    engine._release = release

    worker = threading.Thread(target=engine._worker_loop, daemon=True)
    worker.start()
    try:
        engine._claim(account_id, "fast")
        engine._queue.put(Task(0.0, account_id, "fast"))
        assert survived.wait(timeout=10), "the poisoned task was never completed"

        # The real assertion: the thread is still there to take the next one.
        survived.clear()
        engine._claim(account_id, "fast")
        engine._queue.put(Task(0.0, account_id, "fast"))
        assert survived.wait(timeout=10), (
            "the worker thread died on the first failure and never took another task"
        )
        assert worker.is_alive()
    finally:
        engine.stop()
        worker.join(timeout=10)
