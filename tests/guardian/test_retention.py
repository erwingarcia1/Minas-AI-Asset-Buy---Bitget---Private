"""Bounding the tables that grow on every poll (#60).

`prune_balance_snapshots` was the only retention anywhere. `events`, `alerts`,
`seen_withdrawals`, `seen_transfers`, `seen_fills`, `sms_deliveries`,
`operator_pages` and `permission_snapshots` all grew forever, most of them at
the fast loop's 45-second cadence — and this project has already had one outage
from a query going quadratic over a growing table (`baseline.py` still carries
the comment about the 2026-07-06 dashboard incident).

The interesting part isn't deleting old rows. It's which tables must **not** be
pruned, and which rows must survive inside the ones that are — because getting
either wrong turns a housekeeping job into a silent detection failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from guardian.config import RulesConfig
from guardian.retention import NEVER_PRUNE, prune_all, table_counts
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import SHIPPED_RULES


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def rules():
    return RulesConfig(SHIPPED_RULES)


def _account(db, label="ret"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, exchange, label, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, onboarded_at, updated_at) "
            "VALUES ('00000000-0000-4000-8000-00000000a0a0', 'self', 'bitget', %s, 'independent', 'x', 'y', 'z', '2026-01-01', '2026-01-01')",
            (label,),
        )
        account_id = cur.lastrowid
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))
        return account_id


# --- the ordinary case --------------------------------------------------------

def test_old_rows_go_and_recent_ones_stay(db, rules):
    account_id = _account(db)
    with db.cursor() as cur:
        for age in (5000, 1):
            cur.execute(
                "INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                "VALUES (%s, 'withdrawal_initiated', 'CRITICAL', '{}', %s)",
                (account_id, _ago(age)),
            )

    deleted = prune_all(db, rules)

    assert deleted["guardian_events"] == 1
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_events")
        assert cur.fetchone()["c"] == 1


def test_every_growing_table_is_covered(db, rules):
    """The list is the point. A table added later that nobody adds here grows
    forever and nothing says so."""
    from guardian.retention import KEEP_LATEST_PER_ACCOUNT, PRUNABLE

    covered = set(PRUNABLE) | set(KEEP_LATEST_PER_ACCOUNT) | NEVER_PRUNE
    with db.cursor() as cur:
        cur.execute("SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND starts_with(table_name, 'guardian_')")
        actual = {r["name"] for r in cur.fetchall()}

    assert actual - covered == set(), f"tables with no retention decision: {actual - covered}"


def test_every_retention_decision_names_a_real_table(db, rules):
    """The other direction, and the one that fails silently.

    `prune_all` iterates `PRUNABLE` and issues `DELETE FROM <table> WHERE
    <column> < %s` per key. A key naming a table that was renamed or dropped
    matches nothing — no error, `rowcount` 0 — so the entry reads as active
    retention in the list, in `table_counts`, and to anyone auditing what this
    product keeps, while pruning nothing at all. The table it was *meant* to
    cover grows forever, and `test_every_growing_table_is_covered` above stays
    green throughout, because the stale key still counts as covering it.

    Same shape as the `[1.0.0]` CHANGELOG link definition that outlived its
    heading (see docs/AUTONOMOUS-LOG.md): two sets that are views of one list,
    containment asserted one way only, so an entry pointing at something that
    no longer exists is invisible. Both directions are meaningful here.
    """
    from guardian.retention import KEEP_LATEST_PER_ACCOUNT, PRUNABLE

    covered = set(PRUNABLE) | set(KEEP_LATEST_PER_ACCOUNT) | NEVER_PRUNE
    with db.cursor() as cur:
        cur.execute("SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND starts_with(table_name, 'guardian_')")
        actual = {r["name"] for r in cur.fetchall()}

    assert covered - actual == set(), (
        f"retention decisions naming no such table: {sorted(covered - actual)}. "
        "A renamed or dropped table leaves the old key behind, where it prunes "
        "nothing and still reads as covered."
    )


def test_pruning_is_repeatable(db, rules):
    account_id = _account(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
            "VALUES (%s, 'x', 'LOW', '{}', %s)", (account_id, _ago(5000)))

    assert prune_all(db, rules)["guardian_events"] == 1
    assert prune_all(db, rules)["guardian_events"] == 0


# --- what must never be pruned -----------------------------------------------

def test_baseline_tables_are_never_pruned(db, rules):
    """`known_addresses`, `traded_pairs` and `transfer_routes` are the account's
    baseline, not its history. Deleting an old row there doesn't free space, it
    manufactures a false positive: the next withdrawal to a five-year-old
    address becomes "never seen before", and the customer gets a CRITICAL about
    their own cold wallet."""
    account_id = _account(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_known_addresses (account_id, address, coin, source, first_seen_at) "
            "VALUES (%s, 'bc1qancient', 'BTC', 'withdrawal_history', %s)", (account_id, _ago(5000)))
        cur.execute("INSERT INTO guardian_traded_pairs (account_id, symbol, first_seen_at) VALUES (%s, 'BTC/USDT', %s)",
                    (account_id, _ago(5000)))
        cur.execute("INSERT INTO guardian_transfer_routes (account_id, route, first_seen_at) VALUES (%s, 'spot->swap', %s)",
                    (account_id, _ago(5000)))

    prune_all(db, rules)

    with db.cursor() as cur:
        for table in ("guardian_known_addresses", "guardian_traded_pairs", "guardian_transfer_routes"):
            cur.execute(f"SELECT count(*) c FROM {table}")
            assert cur.fetchone()["c"] == 1, f"{table} was pruned - that is a false-positive generator"


def test_pruning_dedup_state_resurrects_the_alert(db, cipher, monkeypatch):
    """Why the dedup window has to be generous, demonstrated rather than
    asserted.

    `seen_withdrawals` is not history, it is the answer to "have I alerted on
    this". Delete a row while the exchange still returns that withdrawal, and
    the next poll re-alerts on a months-old movement. The retention period for
    these tables is therefore bounded **below by correctness**, not just by
    policy — it must exceed the longest history any exchange returns."""
    from guardian.onboarding import onboard_account
    from guardian.poller import AccountPoller
    from tests.guardian.fixtures.ccxt_responses import withdrawal_record

    class Recording:
        def __init__(self):
            self.sent = []

        def send(self, alert):
            self.sent.append(alert)

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
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="resurrect", topology="independent",
                             api_key="k", secret="s", passphrase="p")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        row = cur.fetchone()

    wd = withdrawal_record(order_id="wd-old", address="bc1qexistingaddress")
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: client(withdrawals=[wd]))
    channel = Recording()
    poller = AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                           {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]})
    poller.run_fast_loop(row)
    assert len([a for a in channel.sent if a.rule == "withdrawal_initiated"]) == 1

    # Simulate a too-short dedup retention.
    with db.cursor() as cur:
        cur.execute("DELETE FROM guardian_seen_withdrawals")

    poller.run_fast_loop(row)

    # Asserted on the detection, not the delivery. The alert cooldown happens to
    # swallow the immediate repeat, which makes this look survivable - it isn't.
    # The detection fires again, writes a second forensic event, and would be
    # delivered the moment the cooldown lapses. On a real deployment the prune
    # and the re-poll are days apart, so there is no cooldown left to help.
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_events WHERE event_type = 'withdrawal_initiated'")
        redetected = cur.fetchone()["c"]

    assert redetected == 2, (
        "expected the months-old withdrawal to be re-detected - this test documents "
        "the hazard the generous dedup default exists to prevent"
    )


def test_the_dedup_window_is_generously_long(rules):
    """Bounded below by correctness. It must exceed the longest withdrawal or
    order history any supported exchange will return on a `since=None` fetch,
    and we do not control that number."""
    assert rules.get_int("retention", "dedup_days") >= 365


# --- what must survive inside a pruned table ---------------------------------

def test_the_latest_snapshot_per_account_always_survives(db, rules):
    """`detect_permission_state_change` returns nothing when there is no
    previous snapshot, so deleting the last one silently resets the baseline —
    a permission change spanning the prune would go undetected. The same is
    true of the balance baseline."""
    account_id = _account(db)
    with db.cursor() as cur:
        for age in (5000, 4000, 3000):
            cur.execute(
                "INSERT INTO guardian_permission_snapshots (account_id, authorities, ip_whitelist, snapshot_at) "
                "VALUES (%s, '\"spot_account_r\"', '\"1.2.3.4\"', %s)", (account_id, _ago(age)))

    prune_all(db, rules)

    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_permission_snapshots WHERE account_id = %s", (account_id,))
        assert cur.fetchone()["c"] == 1, "every snapshot was deleted - the baseline is gone"


def test_the_whole_latest_balance_snapshot_survives_not_just_one_coin(db, rules):
    """A balance snapshot is many rows sharing one timestamp, one per coin.
    Keeping "the newest row" would keep a single coin and silently shrink the
    baseline to it, so every other coin reads as having vanished."""
    account_id = _account(db)
    # One timestamp shared by every coin, which is what `record_balance_snapshot`
    # writes - it computes `now` once and reuses it for the whole snapshot.
    taken_at = _ago(400)
    with db.cursor() as cur:
        for coin in ("BTC", "ETH", "USDT"):
            cur.execute(
                "INSERT INTO guardian_balance_snapshots (account_id, coin, quantity, snapshot_at) VALUES (%s, %s, '1', %s)",
                (account_id, coin, taken_at))

    prune_all(db, rules)

    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_balance_snapshots WHERE account_id = %s", (account_id,))
        assert cur.fetchone()["c"] == 3, "the latest snapshot was partially deleted"


# --- visibility ---------------------------------------------------------------

def test_row_counts_are_reported(db, rules):
    account_id = _account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                    "VALUES (%s, 'x', 'LOW', '{}', %s)", (account_id, _ago(1)))

    counts = table_counts(db)
    assert counts["guardian_events"] == 1
    assert "guardian_seen_fills" in counts


def test_table_sizes_are_exported_as_a_metric(db, rules):
    """Growth has to be observable before it is a problem - this project has
    already had one outage from a table nobody was watching."""
    from prometheus_client import CollectorRegistry

    from guardian.metrics import Metrics

    registry = CollectorRegistry()
    metrics = Metrics(registry=registry)
    account_id = _account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                    "VALUES (%s, 'x', 'LOW', '{}', %s)", (account_id, _ago(1)))

    metrics.set_table_rows(table_counts(db))

    assert registry.get_sample_value("guardian_table_rows", {"table": "guardian_events"}) == 1


# --- wiring -------------------------------------------------------------------

def test_the_scheduler_actually_prunes(db, cipher):
    """The recurring fault in this codebase: behaviour tested, call site not.
    A retention job nobody calls is a table that grows forever."""
    from guardian.engine import Engine

    account_id = _account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                    "VALUES (%s, 'x', 'LOW', '{}', %s)", (account_id, _ago(5000)))

    engine = Engine(db, cipher, RulesConfig(SHIPPED_RULES), {})
    engine._scheduler_tick()

    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_events")
        assert cur.fetchone()["c"] == 0


def test_pruning_does_not_run_on_every_cycle(db, cipher, monkeypatch):
    """Full-table DELETEs on a 45-second loop is wasted I/O on the thing the
    fast loop is querying."""
    from guardian.engine import Engine

    engine = Engine(db, cipher, RulesConfig(SHIPPED_RULES), {})
    calls = {"n": 0}

    def counted(db_, rules_):
        calls["n"] += 1
        return {}

    monkeypatch.setattr("guardian.engine.prune_all", counted)
    for _ in range(5):
        engine._scheduler_tick()

    assert calls["n"] == 1, f"pruned {calls['n']} times in five cycles"


def test_never_run_is_none_not_zero(db, cipher):
    """The monotonic lesson: `time.monotonic()` is seconds since boot, so `0`
    as "never pruned" would gate the first prune on the machine's uptime."""
    from guardian.engine import Engine

    engine = Engine(db, cipher, RulesConfig(SHIPPED_RULES), {})
    assert engine._last_prune is None


# --- alerts and the rows that annotate them -----------------------------------
#
# Three tables carry an `alert_id ... REFERENCES alerts(id)`, `Database` opens
# every connection with `PRAGMA foreign_keys = ON`, and nothing in the schema
# declares `ON DELETE CASCADE`. So the parent delete is refused by SQLite while
# a child still points at it - and because `prune_all` deleted `alerts` before
# any of its children, the refusal aborted the entire retention pass.


def _alert(db, account_id, *, age):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, "
            "evidence_json, confidence, sent_at) "
            "VALUES (%s, 'withdrawal_initiated', 'CRITICAL', 'd', 'm', '{}', 'high', %s)",
            (account_id, _ago(age)),
        )
        return cur.lastrowid


def _triage(db, alert_id, *, age):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alert_triage (alert_id, note, handled_at, handled_by, updated_at) "
            "VALUES (%s, 'working it', %s, 'john', %s)",
            (alert_id, _ago(age), _ago(age)),
        )


def _sms(db, account_id, alert_id, *, age):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_sms_deliveries (account_id, alert_id, rule, phone, status, created_at) "
            "VALUES (%s, %s, 'withdrawal_initiated', '+15550000', 'sent', %s)",
            (account_id, alert_id, _ago(age)),
        )


def _expected(db, account_id, alert_id, *, age):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_expected_activity (alert_id, account_id, rule, dedup_key, marked_by, created_at) "
            "VALUES (%s, %s, 'withdrawal_initiated', 'd', 'customer', %s)",
            (alert_id, account_id, _ago(age)),
        )


def _count(db, table):
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) c FROM {table}")
        return cur.fetchone()["c"]


def test_a_pruned_alert_takes_its_annotations_with_it(db, rules):
    """The rows actually leave. Asserting that `alert_triage` appears in
    PRUNABLE would only re-assert the config; this watches the table empty."""
    account_id = _account(db)
    alert_id = _alert(db, account_id, age=5000)
    _triage(db, alert_id, age=5000)
    _sms(db, account_id, alert_id, age=5000)
    _expected(db, account_id, alert_id, age=5000)

    prune_all(db, rules)

    assert _count(db, "guardian_alerts") == 0
    for child in ("guardian_alert_triage", "guardian_sms_deliveries", "guardian_expected_activity"):
        assert _count(db, child) == 0, f"{child} outlived the alert it annotates"


def test_a_late_touched_triage_row_does_not_abort_the_whole_pass(db, rules):
    """The regression, and the reason this is worth a test at all.

    An alert old enough to prune whose triage row was touched recently is the
    ordinary case - an operator works a queue long after the alert fired. The
    parent delete used to raise IntegrityError, which left `prune_all`
    altogether, so every table sitting after `alerts` in PRUNABLE kept its old
    rows forever and the log said nothing."""
    account_id = _account(db)
    alert_id = _alert(db, account_id, age=5000)
    _triage(db, alert_id, age=1)  # touched yesterday, alert is years old

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_seen_fills (account_id, fill_key, first_seen_at) VALUES (%s, 'f', %s)",
            (account_id, _ago(5000)),
        )

    deleted = prune_all(db, rules)

    assert _count(db, "guardian_alerts") == 0
    assert _count(db, "guardian_alert_triage") == 0
    # seen_fills sits after `alerts` in PRUNABLE: the proof the pass ran on.
    assert deleted["guardian_seen_fills"] == 1
    assert _count(db, "guardian_seen_fills") == 0


def test_a_recent_alert_keeps_its_triage(db, rules):
    """The other direction. A cascade that fires on rows inside the window
    would pass the test above and quietly delete live triage state."""
    account_id = _account(db)
    alert_id = _alert(db, account_id, age=1)
    _triage(db, alert_id, age=1)

    prune_all(db, rules)

    assert _count(db, "guardian_alerts") == 1
    assert _count(db, "guardian_alert_triage") == 1


def test_every_foreign_key_child_of_alerts_is_in_the_cascade(db):
    """The pairs-test for the cascade itself.

    `alert_triage` was the third table to reference `alerts(id)` and the third
    to be left out of the deletion order. A fourth is a schema change away, and
    it would fail exactly as the first three did - at a customer's 730-day
    mark, in a background job, on the deployed database."""
    from guardian.retention import ALERT_CHILDREN

    with db.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT tc.table_name AS name FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage ccu "
            "ON ccu.constraint_name = tc.constraint_name AND ccu.constraint_schema = tc.constraint_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' AND ccu.table_name = 'guardian_alerts'")
        children = {r["name"] for r in cur.fetchall()}

    assert children, "the scan found no children of `alerts` - it is looking in the wrong place"
    assert children == set(ALERT_CHILDREN), (
        f"tables referencing alerts(id) but not cascaded: {sorted(children - set(ALERT_CHILDREN))}; "
        f"cascaded but no longer referencing: {sorted(set(ALERT_CHILDREN) - children)}"
    )


# --- the execution guard ------------------------------------------------------
#
# `test_every_growing_table_is_covered` asks whether somebody made a decision.
# It cannot ask whether the decision executes, and those came apart in exactly
# the way that matters: `alert_triage` was registered in PRUNABLE by b60c65d,
# the registration guard went green, and `prune_all` still could not delete a
# single alert - `PRAGMA foreign_keys = ON` with no `ON DELETE CASCADE` meant
# the parent delete raised, and the raise left `prune_all` altogether, so every
# table after `alerts` stopped being pruned too.
#
# So: one row per table, past its own window, one `prune_all`, and every table
# must be empty afterwards. Table-driven from PRUNABLE rather than a list
# written out by hand, because a hand-written list is the thing that was
# already wrong.

def _stamp(rules, setting, extra_days=30):
    """A timestamp comfortably past `setting`'s window."""
    return _ago(rules.get_int("retention", setting) + extra_days)


# One row per prunable table. Each factory writes its table's own timestamp
# column to `ts`, so the row is unambiguously past that table's window.
_ROW_FACTORIES = {
    "guardian_events": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
        "VALUES (%s, 'withdrawal_initiated', 'CRITICAL', '{}', %s)", (acct, ts)),
    "guardian_alerts": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, "
        "confidence, sent_at) VALUES (%s, 'r', 'HIGH', 'd2', 'm', '{}', 'high', %s)", (acct, ts)),
    "guardian_operator_pages": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_operator_pages (account_id, rule, severity, dedup_key, message, "
        "evidence_json, sent_at) VALUES (%s, 'r', 'HIGH', 'd', 'm', '{}', %s)", (acct, ts)),
    "guardian_sms_deliveries": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_sms_deliveries (account_id, alert_id, rule, phone, status, created_at) "
        "VALUES (%s, %s, 'r', '+15550000', 'sent', %s)", (acct, alert, ts)),
    "guardian_snoozes": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_snoozes (account_id, dedup_key, rule, requested_by, created_at, expires_at) "
        "VALUES (%s, 'd', 'r', 'customer', %s, %s)", (acct, ts, ts)),
    "guardian_alert_triage": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_alert_triage (alert_id, note, handled_at, handled_by, updated_at) "
        "VALUES (%s, 'n', %s, 'john', %s)", (alert, ts, ts)),
    "guardian_config_changes": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_config_changes (setting, old_value, new_value, actor, changed_at) "
        "VALUES ('polling.fast_loop_seconds', '45', '60', 'john', %s)", (ts,)),
    "guardian_expected_activity": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_expected_activity (alert_id, account_id, rule, dedup_key, marked_by, created_at) "
        "VALUES (%s, %s, 'r', 'd', 'customer', %s)", (alert, acct, ts)),
    "guardian_seen_withdrawals": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_seen_withdrawals (account_id, order_id, status, raw_json, first_seen_at) "
        "VALUES (%s, 'o1', 'success', '{}', %s)", (acct, ts)),
    "guardian_seen_transfers": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_seen_transfers (account_id, transfer_id, raw_json, first_seen_at) "
        "VALUES (%s, 't1', '{}', %s)", (acct, ts)),
    "guardian_seen_fills": lambda cur, acct, alert, ts: cur.execute(
        "INSERT INTO guardian_seen_fills (account_id, fill_key, first_seen_at) VALUES (%s, 'f1', %s)",
        (acct, ts)),
}


def test_every_prunable_table_has_a_row_factory():
    """Without this, a table added to PRUNABLE with no factory would be silently
    absent from the execution guard below - which would then prove that eleven
    tables prune while reading as though it proved twelve. The same hole the
    registration guard had, reintroduced one level down."""
    from guardian.retention import PRUNABLE

    assert set(_ROW_FACTORIES) == set(PRUNABLE), (
        f"no row factory for: {sorted(set(PRUNABLE) - set(_ROW_FACTORIES))}; "
        f"factory for a table no longer pruned: {sorted(set(_ROW_FACTORIES) - set(PRUNABLE))}"
    )


def test_every_prunable_table_actually_prunes(db, rules):
    """The execution half. Registration guards need paired execution guards.

    Fails on b60c65d alone with `IntegrityError: FOREIGN KEY constraint failed`,
    because `alerts` is deleted before the three tables that reference it."""
    from guardian.retention import PRUNABLE

    account_id = _account(db)
    with db.cursor() as cur:
        # The parent every child factory needs, aged past the alerts window so
        # it is eligible in the same pass as its children.
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, "
            "confidence, sent_at) VALUES (%s, 'r', 'CRITICAL', 'd1', 'm', '{}', 'high', %s)",
            (account_id, _stamp(rules, "alerts_days")),
        )
        alert_id = cur.lastrowid
        for table, factory in _ROW_FACTORIES.items():
            factory(cur, account_id, alert_id, _stamp(rules, PRUNABLE[table][1]))

    before = {t: _count(db, t) for t in PRUNABLE}
    assert all(before.values()), f"setup failed to populate: {[t for t, n in before.items() if not n]}"

    prune_all(db, rules)

    remaining = {t: _count(db, t) for t in PRUNABLE if _count(db, t)}
    assert remaining == {}, f"past their window and still present: {remaining}"
