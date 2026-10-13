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
