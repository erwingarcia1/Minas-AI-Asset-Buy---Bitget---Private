from __future__ import annotations

from unittest.mock import MagicMock

from guardian.engine import Engine
from tests.guardian.conftest import make_account
from tests.guardian.fixtures.ccxt_responses import account_info_response


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _good_client():
    client = MagicMock()
    client.fetch_deposits.return_value = []
    client.fetch_withdrawals.return_value = []
    client.fetch_closed_orders.return_value = []
    client.fetch_transfers.return_value = []
    client.fetch_tickers.return_value = {}
    client.fetch_balance.return_value = {"total": {"BTC": 1.0}}
    client.fetch_account_info.return_value = account_info_response()
    return client


def _bad_client():
    client = MagicMock()
    client.fetch_deposits.side_effect = RuntimeError("network down")
    return client


def test_one_bad_account_never_stalls_the_fleet(db, cipher, rules, monkeypatch):
    good_ids = [make_account(db, f"good-{i}") for i in range(3)]
    bad_id = make_account(db, "bad-1")
    for account_id in good_ids + [bad_id]:
        with db.cursor() as cur:
            cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))

    def client_for(api_key, secret, password):
        return _good_client()

    clients = {}

    def fake_loader(cipher_arg, row):
        if row["label"] == "bad-1":
            return _bad_client()
        return clients.setdefault(row["id"], _good_client())

    monkeypatch.setattr("guardian.poller._load_client", fake_loader)

    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    engine.run_one_cycle()

    # Every good account polled successfully despite the bad one.
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) c FROM guardian_poll_state WHERE last_success_at IS NOT NULL AND account_id IN (%s)"
            % ",".join(["%s"] * len(good_ids)),
            good_ids,
        )
        assert cur.fetchone()["c"] == len(good_ids)
        cur.execute("SELECT consecutive_failures FROM guardian_poll_state WHERE account_id = %s", (bad_id,))
        assert cur.fetchone()["consecutive_failures"] == 1


def test_an_account_still_seeding_its_baseline_is_not_polled(db, cipher, rules, monkeypatch):
    """Onboarding inserts the row before it seeds the baseline, so a running
    worker could otherwise schedule the account mid-seed and the detectors
    would judge its first poll against half the history. `incomplete` is
    different: that seed finished, with gaps that are recorded and paged, and
    the account is monitored."""
    complete_id = make_account(db, "complete-1")
    incomplete_id = make_account(db, "incomplete-1", baseline_status="incomplete")
    seeding_id = make_account(db, "seeding-1", baseline_status="seeding")
    for account_id in (complete_id, incomplete_id, seeding_id):
        with db.cursor() as cur:
            cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))
    monkeypatch.setattr("guardian.poller._load_client", lambda c, row: _good_client())

    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    assert {row["id"] for row in engine._active_accounts()} == {complete_id, incomplete_id}

    engine.run_one_cycle()

    with db.cursor() as cur:
        cur.execute("SELECT account_id FROM guardian_poll_state WHERE last_success_at IS NOT NULL "
                    "ORDER BY account_id")
        polled = [r["account_id"] for r in cur.fetchall()]
    assert polled == sorted([complete_id, incomplete_id])
    assert engine.snapshot()["active_accounts"] == 2, "a seeding account is not reported as watched"


def test_breaker_opens_for_repeatedly_failing_account(db, cipher, rules, monkeypatch):
    bad_id = make_account(db, "bad-2")
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (bad_id,))

    monkeypatch.setattr("guardian.poller._load_client", lambda c, row: _bad_client())

    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    # rules.yaml test fixture: breaker threshold defaults to 3
    for _ in range(4):
        engine.run_one_cycle()

    assert engine.breakers.for_account(bad_id).is_open


# --- shutdown waits for the scheduler too ------------------------------------

def test_join_waits_for_a_scheduler_pass_that_lingers_after_stop(db, cipher, rules):
    """`run()` starts the scheduler as a daemon thread, and `join()` waited for
    the worker pool only - so a SIGTERM could return, after the final
    "stopping" heartbeat, while the scheduler was still mid-escalation."""
    import threading
    import time

    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    finished = threading.Event()

    def lingering_pass():
        engine._stop.wait()   # the loop notices stop()...
        time.sleep(0.2)       # ...but the pass under way takes a moment to finish
        finished.set()

    engine._scheduler = threading.Thread(target=lingering_pass, daemon=True, name="scheduler")
    engine._scheduler.start()

    engine.stop()
    engine.join(timeout=5)

    assert finished.is_set(), "join() returned while the scheduler pass was still running"
    assert not engine._scheduler.is_alive()


def test_join_gives_up_on_a_stuck_scheduler_within_the_timeout(db, cipher, rules):
    import threading
    import time

    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    stuck = threading.Event()
    engine._scheduler = threading.Thread(target=stuck.wait, daemon=True, name="scheduler")
    engine._scheduler.start()
    engine.stop()

    started = time.monotonic()
    engine.join(timeout=0.3)

    assert time.monotonic() - started < 2, "join() must stay bounded by its timeout"
    stuck.set()


def test_run_keeps_the_scheduler_thread_and_join_reaps_it(db, cipher, rules):
    """Wiring: the thread `run()` starts is the one `join()` waits for."""
    import threading
    import time

    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    runner = threading.Thread(target=engine.run, kwargs={"pool_size": 1}, daemon=True)
    runner.start()
    deadline = time.monotonic() + 5
    while engine._scheduler is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert engine._scheduler is not None and engine._scheduler.name == "scheduler"

    engine.stop()
    engine.join(timeout=5)
    runner.join(5)

    assert not engine._scheduler.is_alive()
    assert not runner.is_alive()
    assert all(not w.is_alive() for w in engine._workers)
