"""#57: one failing alert channel must not cost the alert entirely.

`dispatch()` called `channel.send()` unguarded, and *before* writing the
`alerts` row. A single raising channel therefore meant:

  1. the remaining channels were never tried - including the ones configured
     precisely as backups;
  2. no `alerts` row, so the alert was absent from the customer's history, the
     portal, the dashboard, and anything handed to an exchange as evidence;
  3. `ContactRouter` was never reached, so the customer's own email and phone
     went untried too;
  4. the exception aborted the rest of that account's poll cycle;
  5. one log line, from a handler whose message is about *task failure*.

A flaky Telegram API meant the customer was told nothing, on any channel, with
no record - every cycle, for as long as it lasted.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from guardian.alerts.dispatch import dispatch
from guardian.config import RulesConfig
from guardian.detectors.base import Detection
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller
from tests.guardian.fixtures.ccxt_responses import account_info_response, withdrawal_record
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import make_user
from tests.guardian.conftest import SHIPPED_RULES

COOLDOWNS = {"CRITICAL": 15, "HIGH": 30, "MEDIUM": 720, "LOW": 1440}


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class Broken:
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("telegram is down")
        self.attempts = 0

    def send(self, alert):
        self.attempts += 1
        raise self.exc


def _account(db, label="disp"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, exchange, label, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, alert_phone, status, onboarded_at, updated_at) "
            "VALUES (%s, 'self', 'bitget', %s, 'independent', 'x', 'y', 'z', '+15550000000', "
            "'active', '2026-01-01', '2026-01-01')",
            (make_user(db, "c@example.com"), label),
        )
        return cur.lastrowid


def _detection(rule="withdrawal_initiated", severity="CRITICAL"):
    return Detection(rule=rule, severity=severity, confidence="high",
                     message="0.5 BTC withdrawal", evidence={"order_id": "wd-1"})


# --- one dead channel must not take the others -------------------------------

def test_a_failing_channel_does_not_stop_the_others(db):
    """CRITICAL routes to telegram, telegram_team, sms and email. If Telegram
    is first and down, the other three are exactly what should still fire."""
    account_id = _account(db)
    broken, first, second = Broken(), Recording(), Recording()

    dispatch(db, {"CRITICAL": [broken, first, second]}, account_id, "acct", _detection(), COOLDOWNS)

    assert broken.attempts == 1
    assert len(first.sent) == 1, "a later channel was skipped because an earlier one failed"
    assert len(second.sent) == 1


def test_a_failing_channel_does_not_raise(db):
    """The exception used to escape dispatch, through _emit, out of
    run_fast_loop, and into the worker's blanket handler - abandoning every
    detector that had not yet run for that account."""
    account_id = _account(db)
    dispatch(db, {"CRITICAL": [Broken()]}, account_id, "acct", _detection(), COOLDOWNS)


# --- detected is not the same fact as delivered ------------------------------

def test_the_alert_is_recorded_even_when_every_channel_fails(db):
    """"We detected this" and "we managed to tell someone" are different facts
    and must stop sharing a row's existence. An alert nobody received still
    happened, and the customer's history is evidence."""
    account_id = _account(db)

    alert_id = dispatch(db, {"CRITICAL": [Broken(), Broken()]}, account_id, "acct",
                        _detection(), COOLDOWNS)

    assert alert_id is not None
    with db.cursor() as cur:
        cur.execute("SELECT rule, delivered_to FROM guardian_alerts WHERE id = %s", (alert_id,))
        row = cur.fetchone()
    assert row["rule"] == "withdrawal_initiated"
    assert row["delivered_to"] == 0, "an undelivered alert must be distinguishable"


def test_a_delivered_alert_records_how_many_channels_took_it(db):
    account_id = _account(db)

    alert_id = dispatch(db, {"CRITICAL": [Recording(), Broken(), Recording()]}, account_id,
                        "acct", _detection(), COOLDOWNS)

    with db.cursor() as cur:
        cur.execute("SELECT delivered_to FROM guardian_alerts WHERE id = %s", (alert_id,))
        assert cur.fetchone()["delivered_to"] == 2


def test_dedup_still_applies_to_an_undelivered_alert(db):
    """The row is what dedup reads, so recording an undelivered alert must not
    turn a flaky channel into a repeat-alert storm."""
    account_id = _account(db)
    dispatch(db, {"CRITICAL": [Broken()]}, account_id, "acct", _detection(), COOLDOWNS)
    second = dispatch(db, {"CRITICAL": [Recording()]}, account_id, "acct", _detection(), COOLDOWNS)

    assert second is None, "the same detection fired twice inside its cooldown"


# --- the poll carries on ------------------------------------------------------

def test_a_delivery_failure_does_not_abandon_the_rest_of_the_poll(db, cipher, monkeypatch):
    """The one that costs the most. A withdrawal alert whose channel is down
    used to take every later detector on that account down with it."""
    def client(withdrawals=None, fills=None):
        c = MagicMock()
        c.fetch_deposits.return_value = []
        c.fetch_withdrawals.return_value = withdrawals or []
        c.fetch_closed_orders.return_value = fills or []
        c.fetch_balance.return_value = {"total": {"BTC": 1.0}}
        c.fetch_account_info.return_value = account_info_response()
        c.fetch_tickers.return_value = {}
        c.fetch_transfers.return_value = []
        return c

    # Onboarding sees a quiet account, so the poll below is the first sighting
    # of both records - otherwise seed_baseline knows them and nothing fires.
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="carry-on", topology="independent",
                             api_key="k", secret="s", passphrase="p")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        row = cur.fetchone()

    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: client(
        withdrawals=[withdrawal_record(order_id="wd-1", address="bc1qneveraddress")],
        fills=[{
            "id": "f-1", "symbol": "SHIB/USDT", "side": "sell", "average": "1", "price": "1",
            "amount": "1", "filled": "1", "cost": "1", "fee": {"currency": "USDT", "cost": "1"},
            "timestamp": 1_900_000_000_000, "info": {},
        }],
    ))
    good = Recording()
    # CRITICAL is dead; HIGH still works. The new-pair detection is HIGH and
    # runs *after* the withdrawal detection that fails.
    poller = AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                           {"CRITICAL": [Broken()], "HIGH": [good], "MEDIUM": [], "LOW": []})
    poller.run_fast_loop(row)

    assert "never_touched_pair_activity" in {a.rule for a in good.sent}, (
        "a failed CRITICAL delivery stopped the detectors that run after it"
    )


def test_the_per_account_contact_router_is_still_reached(db, cipher, monkeypatch):
    """_emit calls dispatch and *then* the per-account router, so a raising
    channel used to cost the customer's own email and phone as well."""
    router = MagicMock()

    def client(withdrawals=None):
        c = MagicMock()
        c.fetch_deposits.return_value = []
        c.fetch_withdrawals.return_value = withdrawals or []
        c.fetch_closed_orders.return_value = []
        c.fetch_balance.return_value = {"total": {"BTC": 1.0}}
        c.fetch_account_info.return_value = account_info_response()
        c.fetch_tickers.return_value = {}
        c.fetch_transfers.return_value = []
        return c

    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="router", topology="independent",
                             api_key="k", secret="s", passphrase="p")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        row = cur.fetchone()

    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: client(
        withdrawals=[withdrawal_record(order_id="wd-9", address="bc1qneveraddress")]
    ))
    AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                  {"CRITICAL": [Broken()], "HIGH": [], "MEDIUM": [], "LOW": []},
                  contact_router=router).run_fast_loop(row)

    assert router.send.call_count >= 1, (
        "the customer's own email and phone were never attempted"
    )


# --- and somebody is told -----------------------------------------------------

def test_every_channel_failing_raises_an_operator_page(db, cipher):
    """The case where a customer is genuinely uninformed. It has somewhere to
    go now (#53), and it is the difference between a degraded delivery and a
    silent one."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    account_id = _account(db, "pageme")
    ops = Recording()
    alerter = OperatorAlerter(db, {"CRITICAL": [ops]}, RulesConfig(SHIPPED_RULES))
    poller = AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                           {"CRITICAL": [Broken()], "HIGH": [], "MEDIUM": [], "LOW": []},
                           operator_alerter=alerter)

    poller._emit(account_id, "pageme", _detection())

    pages = recent_operator_pages(db)
    assert len(pages) == 1
    assert pages[0]["account_id"] == account_id
    assert "withdrawal_initiated" in pages[0]["message"]


def test_a_partly_delivered_alert_does_not_page(db, cipher):
    """One channel taking it means the customer was reached. Paging there would
    train an operator to ignore the channel."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    account_id = _account(db, "nopage")
    alerter = OperatorAlerter(db, {"CRITICAL": [Recording()]}, RulesConfig(SHIPPED_RULES))
    poller = AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                           {"CRITICAL": [Broken(), Recording()], "HIGH": [], "MEDIUM": [], "LOW": []},
                           operator_alerter=alerter)

    poller._emit(account_id, "nopage", _detection())

    assert recent_operator_pages(db) == []


def test_no_configured_channel_at_all_also_pages(db, cipher):
    """Distinct from a channel failing: a severity with nothing routed to it
    delivers nowhere, and used to do so completely silently."""
    from guardian.alerts.operator import OperatorAlerter, recent_operator_pages

    account_id = _account(db, "nochannels")
    alerter = OperatorAlerter(db, {"CRITICAL": [Recording()]}, RulesConfig(SHIPPED_RULES))
    poller = AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                           {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []},
                           operator_alerter=alerter)

    poller._emit(account_id, "nochannels", _detection())

    assert len(recent_operator_pages(db)) == 1
