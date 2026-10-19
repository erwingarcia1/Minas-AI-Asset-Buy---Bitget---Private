from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from guardian.engine import RESEED_MAX_PER_PASS, Engine
from tests.guardian.conftest import TEST_USER_ID, make_account
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


# --- a row left 'seeding' is re-seeded, not forgotten ----------------------------------


def _seeding_account(db, cipher, label, updated_at, *, status="active"):
    """A row as the API leaves it between the 201 and its background seed:
    real ciphertexts (the re-seed decrypts them), 'seeding', a poll_state row."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, label, exchange, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, status, baseline_status, onboarding_source, onboarded_at, updated_at) "
            "VALUES (%s, %s, 'bitget', 'independent', %s, %s, %s, %s, 'seeding', 'self', %s, %s)",
            (TEST_USER_ID, label, cipher.encrypt(f"{label}-key"), cipher.encrypt(f"{label}-secret"),
             cipher.encrypt("p"), status, updated_at.isoformat(), updated_at.isoformat()),
        )
        account_id = cur.lastrowid
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))
        return account_id


def _baseline_status(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT baseline_status FROM guardian_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()["baseline_status"]


def _queued(engine, kind="reseed"):
    return [t.account_id for t in list(engine._queue.queue) if t.kind == kind]


class _Pager:
    def __init__(self):
        self.pages = []

    def page(self, **kwargs):
        self.pages.append(kwargs)
        return 1


def test_a_row_left_seeding_too_long_is_reseeded_from_the_worker_pool(db, cipher, rules, monkeypatch, caplog):
    """The API inserts the row 'seeding' and seeds in a background task. If the
    API died in between, the row stayed 'seeding' forever: the scheduler skips
    seeding rows, so the account was never polled and nothing said so. The
    scheduler now re-seeds a row that has said 'seeding' for 15 minutes - as a
    queued task for the pool, never on its own thread - and leaves fresher and
    switched-off rows alone."""
    caplog.set_level(logging.INFO, logger="guardian.engine")
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("guardian.engine._utcnow", lambda: now)
    stale = _seeding_account(db, cipher, "stale", now - timedelta(minutes=16))
    fresh = _seeding_account(db, cipher, "fresh", now - timedelta(minutes=14))
    off = _seeding_account(db, cipher, "off", now - timedelta(hours=2), status="disabled")
    made = []

    def ctor(exchange, api_key, secret, passphrase=""):
        made.append((exchange, api_key, secret, passphrase))
        return _good_client()

    monkeypatch.setattr("guardian.onboarding.make_client", ctor)
    monkeypatch.setattr("guardian.poller._load_client", lambda c, row: _good_client())
    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})

    engine._schedule_cycle()

    assert [(t.account_id, t.kind) for t in list(engine._queue.queue)] == [(stale, "reseed")], \
        "queued for the pool, not run on the scheduler thread"
    assert _baseline_status(db, stale) == "seeding", "nothing has run yet"
    assert made == []
    engine._schedule_cycle()
    assert _queued(engine) == [stale], "one re-seed in flight per account, however many passes go by"
    assert "account stale:" in caplog.text and "re-seeding" in caplog.text
    assert "account fresh:" not in caplog.text and "account off:" not in caplog.text

    engine._drain_once()

    assert _baseline_status(db, stale) == "complete"
    assert made == [("bitget", "stale-key", "stale-secret", "p")], "the client is rebuilt from the stored credentials"
    assert _baseline_status(db, fresh) == "seeding"
    assert _baseline_status(db, off) == "seeding"
    assert (stale, "reseed") not in engine._inflight
    assert "re-seeded, baseline is complete" in caplog.text

    # From the next pass the account is an ordinary watched one.
    engine._schedule_cycle()
    assert _queued(engine) == []
    assert _queued(engine, "fast") == [stale]
    assert {row["id"] for row in engine._active_accounts()} == {stale}


def test_at_most_a_few_stale_rows_are_reseeded_per_pass_oldest_first(db, cipher, rules, monkeypatch):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("guardian.engine._utcnow", lambda: now)
    ids = [_seeding_account(db, cipher, f"stale-{i}", now - timedelta(hours=1, minutes=i)) for i in range(5)]
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _good_client())
    monkeypatch.setattr("guardian.poller._load_client", lambda c, row: _good_client())
    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})

    engine._schedule_cycle()
    assert RESEED_MAX_PER_PASS == 3
    assert _queued(engine) == [ids[4], ids[3], ids[2]], "oldest first, and no more than the cap"
    engine._drain_once()
    assert [_baseline_status(db, i) for i in ids] == ["seeding", "seeding", "complete", "complete", "complete"]

    engine._schedule_cycle()
    assert _queued(engine) == [ids[1], ids[0]]
    engine._drain_once()
    assert all(_baseline_status(db, i) == "complete" for i in ids)


def test_a_reseed_that_fails_marks_the_row_incomplete_and_pages(db, cipher, rules, monkeypatch):
    """`seed_account_baseline`'s guarantee holds on this path too: the row ends
    'incomplete' with a 'baseline_seed_failed' event, the engine's operator
    alerter is paged, nobody is named as the actor, and the account is polled
    from the next pass."""
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("guardian.engine._utcnow", lambda: now)
    stale = _seeding_account(db, cipher, "unlucky", now - timedelta(hours=1))

    def explode(*a, **k):
        raise RuntimeError("ccxt could not build the client")

    monkeypatch.setattr("guardian.onboarding.make_client", explode)
    monkeypatch.setattr("guardian.poller._load_client", lambda c, row: _good_client())
    pager = _Pager()
    engine = Engine(db, cipher, rules, {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}, operator_alerter=pager)

    engine.run_one_cycle()

    assert _baseline_status(db, stale) == "incomplete"
    with db.cursor() as cur:
        cur.execute("SELECT event_type, actor FROM guardian_events WHERE account_id = %s ORDER BY id", (stale,))
        events = [dict(r) for r in cur.fetchall()]
    assert [e["event_type"] for e in events] == ["baseline_seed_failed"]
    assert events[0]["actor"] is None, "nobody did this"
    assert [p["rule"] for p in pager.pages] == ["account_baseline_seed_failed"]
    assert pager.pages[0]["account_id"] == stale
    assert {row["id"] for row in engine._active_accounts()} == {stale}
