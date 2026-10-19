from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from guardian.config import RulesConfig
from guardian.db import Database
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller, _update_poll_state
from tests.guardian.fixtures.ccxt_responses import account_info_response, trade_record, withdrawal_record
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import SHIPPED_RULES
from guardian.db import platform_dsn
import os


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _mock_client(deposits=None, withdrawals=None, fills=None, balance_total=None, authorities="stor"):
    client = MagicMock()
    client.fetch_deposits.return_value = deposits or []
    client.fetch_withdrawals.return_value = withdrawals or []
    client.fetch_closed_orders.return_value = fills or []
    client.fetch_balance.return_value = {"total": balance_total or {}}
    client.fetch_account_info.return_value = account_info_response(authorities=authorities)
    return client


def test_end_to_end_new_withdrawal_to_unseen_address_fires_critical(db, cipher, monkeypatch):
    """Onboards a fake account, then runs one fast-loop cycle where a brand
    new withdrawal to a never-seen address shows up - both the
    withdrawal-created and unseen-address detectors should fire, get
    logged to the append-only event log, and reach the alert channel."""
    onboarding_client = _mock_client(balance_total={"BTC": 1.0})
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: onboarding_client)
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-1", topology="independent", api_key="k", secret="s", passphrase="p"
    )

    poll_client = _mock_client(
        balance_total={"BTC": 1.0},
        withdrawals=[withdrawal_record(order_id="wd-new", address="bc1qneveraddress", amount="0.1")],
    )
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: poll_client)

    channel = RecordingChannel()
    rules = RulesConfig(SHIPPED_RULES)
    poller = AccountPoller(
        db, cipher, rules, {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]}
    )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()

    poller.run_fast_loop(account_row)

    fired_rules = {a.rule for a in channel.sent}
    assert "withdrawal_initiated" in fired_rules
    assert "withdrawal_to_unseen_address" in fired_rules

    with db.cursor() as cur:
        cur.execute("SELECT event_type FROM guardian_events WHERE account_id = %s", (result.account_id,))
        event_types = {r["event_type"] for r in cur.fetchall()}
    assert "withdrawal_initiated" in event_types
    assert "withdrawal_to_unseen_address" in event_types


def test_end_to_end_repeat_withdrawal_does_not_refire(db, cipher, monkeypatch):
    """A withdrawal already seen in a prior cycle must not fire again."""
    onboarding_client = _mock_client(balance_total={"BTC": 1.0})
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: onboarding_client)
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-2", topology="independent", api_key="k", secret="s", passphrase="p"
    )

    existing_withdrawal = withdrawal_record(order_id="wd-existing", address="bc1qexistingaddress", amount="0.1")
    poll_client = _mock_client(balance_total={"BTC": 1.0}, withdrawals=[existing_withdrawal])
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: poll_client)

    channel = RecordingChannel()
    rules = RulesConfig(SHIPPED_RULES)
    poller = AccountPoller(
        db, cipher, rules, {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]}
    )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()

    poller.run_fast_loop(account_row)  # first sighting -> fires
    poller.run_fast_loop(account_row)  # same withdrawal again -> must not refire

    withdrawal_alerts = [a for a in channel.sent if a.rule == "withdrawal_initiated"]
    assert len(withdrawal_alerts) == 1


def test_stale_sale_does_not_explain_a_fresh_drop(db, cipher, monkeypatch):
    """G-7: a large legitimate sale from a week ago must not explain away a
    theft-sized balance drop that happened inside the last 45-second cycle.

    The balance drop is measured between two consecutive fast-loop snapshots,
    but `fetch_closed_orders` is called with `since=None`, so it returns the
    exchange's full default history. Every one of those old fills is then
    counted as an explanation, which lets a real drain pass silently."""
    onboarding_client = _mock_client(balance_total={"BTC": 1.0})
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: onboarding_client)
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-1", topology="independent", api_key="k", secret="s", passphrase="p"
    )

    # Pretend the previous fast loop ran 45s ago. This is what the off-market
    # and fee detectors already use to window their input; the quantity-drop
    # path ignores it. Setting it keeps those detectors out of the way so this
    # test isolates the drop path.
    now = datetime.now(timezone.utc)
    _update_poll_state(
        db, result.account_id, last_fast_loop_at=(now - timedelta(seconds=45)).isoformat()
    )

    # A legitimate 0.5 BTC sale from seven days ago. It was already settled and
    # already reflected in the baseline snapshot taken at onboarding, so it
    # cannot account for any part of a drop measured from that snapshot.
    stale_sale = trade_record(symbol="BTC/USDT", side="sell", amount="0.5", cost="30000")
    stale_sale["timestamp"] = int((now - timedelta(days=7)).timestamp() * 1000)

    # Balance falls 1.0 -> 0.5 BTC inside this cycle, with no withdrawal and no
    # fill in the window: a theft-sized, wholly unexplained drop.
    poll_client = _mock_client(balance_total={"BTC": 0.5}, fills=[stale_sale])
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: poll_client)

    channel = RecordingChannel()
    rules = RulesConfig(SHIPPED_RULES)
    poller = AccountPoller(
        db, cipher, rules, {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]}
    )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()

    poller.run_fast_loop(account_row)

    fired_rules = {a.rule for a in channel.sent}
    assert "unexplained_quantity_decrease" in fired_rules, (
        "a 0.5 BTC drop in the last 45s was explained away by a sale from 7 days ago"
    )


def test_recent_sale_still_explains_a_drop(db, cipher, monkeypatch):
    """Converse of the test above, and the reason it can't pass vacuously: a
    sale that really did happen inside the window must still suppress the
    alert. Without this, a window computed so badly that nothing ever explains
    a drop would look like a passing fix while alerting on every legitimate
    trade."""
    onboarding_client = _mock_client(balance_total={"BTC": 1.0})
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: onboarding_client)
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-1", topology="independent", api_key="k", secret="s", passphrase="p"
    )

    now = datetime.now(timezone.utc)
    _update_poll_state(
        db, result.account_id, last_fast_loop_at=(now - timedelta(seconds=45)).isoformat()
    )

    # Same 0.5 BTC drop, but the customer genuinely sold 10 seconds ago.
    fresh_sale = trade_record(symbol="BTC/USDT", side="sell", amount="0.5", cost="30000")
    fresh_sale["timestamp"] = int((now - timedelta(seconds=10)).timestamp() * 1000)

    poll_client = _mock_client(balance_total={"BTC": 0.5}, fills=[fresh_sale])
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: poll_client)

    channel = RecordingChannel()
    rules = RulesConfig(SHIPPED_RULES)
    poller = AccountPoller(
        db, cipher, rules, {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]}
    )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()

    poller.run_fast_loop(account_row)

    fired_rules = {a.rule for a in channel.sent}
    assert "unexplained_quantity_decrease" not in fired_rules, (
        "a sale from 10s ago should still explain the drop it caused"
    )


# --- Restart coverage (G-7) -------------------------------------------------
# Deploys are a manual `docker compose up -d --build` and the process handles
# no SIGTERM, so it dies mid-cycle every release. Nothing in the drop path may
# depend on process lifetime: the window anchors on the last balance snapshot,
# which is in SQLite, so it survives the restart that produced the gap. These
# tests pin that down - it is the property that makes an outage a delay in
# detection rather than a hole in it.


def _simulate_downtime(db, account_id, minutes):
    """Backdate the persisted poll state so the account looks like it was last
    seen `minutes` ago and the process has just come back up. Rewrites both
    anchors, so a regression to `last_fast_loop_at` can't pass by accident."""
    was_at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_balance_snapshots SET snapshot_at = %s WHERE account_id = %s", (was_at, account_id))
        cur.execute("UPDATE guardian_poll_state SET last_fast_loop_at = %s WHERE account_id = %s", (was_at, account_id))
    return was_at


def _restarted_poller(db_path, cipher, channel):
    """A poller on a *new* Database handle over the same file - what the next
    container gets. Anything the old process held in memory is gone."""
    restarted_db = Database(platform_dsn(os.environ["DATABASE_URL"]))   # a new handle: nothing in memory survives
    rules = RulesConfig(SHIPPED_RULES)
    poller = AccountPoller(
        restarted_db, cipher, rules,
        {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]},
    )
    return restarted_db, poller


def _onboard_and_settle(db, cipher, monkeypatch, tmp_path):
    """Onboard at 1.0 BTC and run one clean cycle, so the account is in the
    steady state a restart would actually interrupt - a real snapshot written
    by a real fast loop, not just the onboarding seed."""
    onboarding_client = _mock_client(balance_total={"BTC": 1.0})
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: onboarding_client)
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-1", topology="independent", api_key="k", secret="s", passphrase="p"
    )

    quiet_client = _mock_client(balance_total={"BTC": 1.0})
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: quiet_client)
    rules = RulesConfig(SHIPPED_RULES)
    poller = AccountPoller(db, cipher, rules, {sev: [] for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")})
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()
    poller.run_fast_loop(account_row)
    return result.account_id, account_row


def test_theft_during_a_restart_is_detected_on_the_first_poll_after_startup(db, cipher, monkeypatch, tmp_path):
    """The headline property: a drain that happens while the service is down
    is caught by the first poll after it comes back.

    It holds because the drop is measured from the last *snapshot*, which is
    in SQLite. The restarted process has no memory of the old one, and does
    not need any: it compares a live balance against the last thing the
    previous process durably recorded."""
    account_id, account_row = _onboard_and_settle(db, cipher, monkeypatch, tmp_path)
    _simulate_downtime(db, account_id, minutes=20)

    # 20 minutes down. The account was drained to 0.5 BTC while nothing was
    # watching, by a route that leaves no withdrawal or fill we can see.
    channel = RecordingChannel()
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _mock_client(balance_total={"BTC": 0.5}))
    restarted_db, poller = _restarted_poller(tmp_path / "test.db", cipher, channel)
    try:
        poller.run_fast_loop(account_row)
    finally:
        restarted_db.close()

    fired = {a.rule for a in channel.sent}
    assert "unexplained_quantity_decrease" in fired, (
        "a 0.5 BTC drain during a 20-minute outage went unreported on the first poll back"
    )


def test_legitimate_activity_during_a_restart_still_explains_the_drop(db, cipher, monkeypatch, tmp_path):
    """The converse, and the reason the test above can't pass vacuously: the
    window has to *widen* to cover the outage, not merely start late. A sale
    the customer made while we were down must still explain its own drop, or
    every deploy would page them."""
    account_id, account_row = _onboard_and_settle(db, cipher, monkeypatch, tmp_path)
    _simulate_downtime(db, account_id, minutes=20)

    # Sold 10 minutes ago - inside the outage, after the last snapshot.
    sale = trade_record(symbol="BTC/USDT", side="sell", amount="0.5", cost="30000")
    sale["timestamp"] = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)

    channel = RecordingChannel()
    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _mock_client(balance_total={"BTC": 0.5}, fills=[sale]),
    )
    restarted_db, poller = _restarted_poller(tmp_path / "test.db", cipher, channel)
    try:
        poller.run_fast_loop(account_row)
    finally:
        restarted_db.close()

    fired = {a.rule for a in channel.sent}
    assert "unexplained_quantity_decrease" not in fired, (
        "a sale made during the outage should still explain the drop it caused"
    )


def test_pre_outage_activity_does_not_explain_a_theft_during_the_outage(db, cipher, monkeypatch, tmp_path):
    """The two properties together, which is where a regression would hide.

    An outage necessarily widens the explanation window - that is what makes
    the test above pass. This one proves it widens to the outage and no
    further: a sale from before the last snapshot is still refused as an
    explanation, so G-7 doesn't quietly come back the moment the service
    restarts."""
    account_id, account_row = _onboard_and_settle(db, cipher, monkeypatch, tmp_path)
    _simulate_downtime(db, account_id, minutes=20)

    # Settled and already reflected in the snapshot the drop is measured from,
    # so it cannot account for any part of that drop.
    old_sale = trade_record(symbol="BTC/USDT", side="sell", amount="0.5", cost="30000")
    old_sale["timestamp"] = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)

    channel = RecordingChannel()
    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _mock_client(balance_total={"BTC": 0.5}, fills=[old_sale]),
    )
    restarted_db, poller = _restarted_poller(tmp_path / "test.db", cipher, channel)
    try:
        poller.run_fast_loop(account_row)
    finally:
        restarted_db.close()

    fired = {a.rule for a in channel.sent}
    assert "unexplained_quantity_decrease" in fired, (
        "a week-old sale explained away a drain that happened during the outage"
    )
