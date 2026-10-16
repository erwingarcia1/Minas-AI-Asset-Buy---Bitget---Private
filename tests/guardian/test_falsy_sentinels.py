"""Absent values that silently became specific wrong ones.

Third and fourth instances of one pattern, found by sweeping for it rather than
by tripping over it:

  - `(f.get("timestamp") or 0) >= last_run_ms`        undated fill never checked   [G-47]
  - `evidence["order_id"] or evidence["coin"]`        two withdrawals, one alert   [G-11]
  - `self._last_slow.get(account_id, 0)`              slow loop gated on uptime    [today]

In each, *"I don't have this value"* became a specific wrong value instead of an
unknown, and the wrong value was plausible enough to pass review. These are the
two the sweep turned up, and both are the same bug in places the earlier fixes
didn't look.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from guardian.alerts.dispatch import dispatch
from guardian.baseline import record_seen_transfer
from guardian.config import RulesConfig
from guardian.detectors.base import Detection
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import SHIPPED_RULES

COOLDOWNS = {"CRITICAL": 15, "HIGH": 30, "MEDIUM": 720, "LOW": 1440}


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _account_row(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, exchange, label, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, onboarded_at, updated_at) "
            "VALUES ('00000000-0000-4000-8000-00000000a0a0', 'self', 'bitget', 'falsy', 'independent', 'x', 'y', 'z', '2026-01-01', '2026-01-01')"
        )
        return cur.lastrowid


# --- record_seen_transfer: G-11's twin, left standing ------------------------

def test_a_transfer_with_no_id_is_not_treated_as_already_seen(db):
    """`record_seen_transfer` is `record_seen_withdrawal` verbatim: an id-less
    record returns False, which the poller reads as "already seen", so it never
    reaches `detect_new_transfer_route`.

    G-11 fixed exactly this for withdrawals and left the twin untouched."""
    account_id = _account_row(db)
    transfer = {"fromAccount": "spot", "toAccount": "swap", "currency": "USDT",
                "amount": 100, "timestamp": 1_700_000_000_000}

    assert record_seen_transfer(db, account_id, transfer) is True, (
        "an id-less transfer was reported as already seen and dropped"
    )


def test_the_same_id_less_transfer_is_only_new_once(db):
    account_id = _account_row(db)
    transfer = {"fromAccount": "spot", "toAccount": "swap", "currency": "USDT",
                "amount": 100, "timestamp": 1_700_000_000_000}

    assert record_seen_transfer(db, account_id, transfer) is True
    assert record_seen_transfer(db, account_id, transfer) is False


def test_two_distinct_id_less_transfers_are_both_new(db):
    account_id = _account_row(db)
    first = {"fromAccount": "spot", "toAccount": "swap", "currency": "USDT",
             "amount": 100, "timestamp": 1_700_000_000_000}
    second = {"fromAccount": "spot", "toAccount": "p2p", "currency": "USDT",
              "amount": 250, "timestamp": 1_700_000_500_000}

    assert record_seen_transfer(db, account_id, first) is True
    assert record_seen_transfer(db, account_id, second) is True


def test_an_id_less_transfer_reaches_the_route_detector(db, cipher, monkeypatch):
    """End to end: a never-used route on an id-less transfer must still alert.
    An attacker moving funds spot -> a route the customer never uses is the
    whole point of this detector."""
    def client(transfers=None):
        c = MagicMock()
        c.fetch_deposits.return_value = []
        c.fetch_withdrawals.return_value = []
        c.fetch_closed_orders.return_value = []
        c.fetch_balance.return_value = {"total": {"USDT": 500.0}}
        c.fetch_account_info.return_value = account_info_response()
        c.fetch_tickers.return_value = {}
        c.fetch_transfers.return_value = transfers or []
        return c

    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="tr", topology="independent",
                             api_key="k", secret="s", passphrase="p")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        row = cur.fetchone()

    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: client(transfers=[
        {"fromAccount": "spot", "toAccount": "p2p", "currency": "USDT",
         "amount": 500, "timestamp": 1_900_000_000_000}
    ]))
    channel = Recording()
    AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                  {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel],
                   "LOW": [channel]}).run_fast_loop(row)

    assert "transfer_route_never_seen" in {a.rule for a in channel.sent}


# --- dedup key: "" is not an identity ----------------------------------------

def _off_market(symbol, order_id=None):
    """Exactly what `detect_off_market_fills` produces: `symbol`, and an
    `order_id` that is None for a fill the exchange didn't reference. Note
    there is no `coin` key - which is what `_dedup_key` falls back to."""
    return Detection(
        rule="off_market_execution", severity="HIGH", confidence="high",
        message=f"Fill on {symbol} executed far from reference price",
        evidence={"symbol": symbol, "order_id": order_id, "fill_price": "0.5",
                  "reference_price": "1.0"},
    )


def test_two_off_market_fills_on_different_symbols_both_alert(db):
    """`_dedup_key` used `order_id or coin or ""`. An off-market detection
    carries `symbol`, not `coin`, and `order_id` is None for an id-less fill -
    so the key collapsed to the empty string and every off-market fill on the
    account shared one.

    G-47 is what made this reachable: before it, id-less fills were never
    judged at all, so they never got as far as being deduped."""
    account_id = _account_row(db)
    channel = Recording()
    channels = {"HIGH": [channel]}

    dispatch(db, channels, account_id, "acct", _off_market("AAA/USDT"), COOLDOWNS)
    dispatch(db, channels, account_id, "acct", _off_market("BBB/USDT"), COOLDOWNS)

    assert len(channel.sent) == 2, (
        "two off-market fills on different symbols collapsed into one alert"
    )


def test_the_same_off_market_symbol_still_dedups(db):
    """The other half: distinguishing them must not stop a genuine repeat being
    suppressed, or a flapping price storms the customer."""
    account_id = _account_row(db)
    channel = Recording()
    channels = {"HIGH": [channel]}

    dispatch(db, channels, account_id, "acct", _off_market("AAA/USDT"), COOLDOWNS)
    dispatch(db, channels, account_id, "acct", _off_market("AAA/USDT"), COOLDOWNS)

    assert len(channel.sent) == 1


def test_two_new_pairs_both_alert(db):
    """Same shape: `never_touched_pair_activity` carries `symbol` only."""
    account_id = _account_row(db)
    channel = Recording()
    channels = {"HIGH": [channel]}

    for symbol in ("SHIB/USDT", "DOGE/USDT"):
        dispatch(db, channels, account_id, "acct", Detection(
            rule="never_touched_pair_activity", severity="HIGH", confidence="high",
            message=f"activity on {symbol}", evidence={"symbol": symbol},
        ), COOLDOWNS)

    assert len(channel.sent) == 2


def test_a_detection_with_no_identifying_evidence_still_dedups_by_rule(db):
    """The floor. When there is genuinely nothing to identify a detection by,
    collapsing on rule + account is the right answer - that is one condition,
    not many."""
    account_id = _account_row(db)
    channel = Recording()
    channels = {"MEDIUM": [channel]}

    for _ in range(2):
        dispatch(db, channels, account_id, "acct", Detection(
            rule="something_vague", severity="MEDIUM", confidence="low",
            message="hmm", evidence={},
        ), COOLDOWNS)

    assert len(channel.sent) == 1
