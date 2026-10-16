from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from guardian.baseline import (
    is_known_address,
    known_transfer_routes,
    latest_balance_snapshot,
    prune_balance_snapshots,
    record_balance_snapshot,
    record_known_address,
    record_seen_withdrawal,
    seed_baseline,
)
from tests.guardian.conftest import make_account
from tests.guardian.fixtures.ccxt_responses import withdrawal_record


def test_seen_withdrawal_is_reported_new_only_once(db):
    account_id = make_account(db)
    record = withdrawal_record(order_id="wd-1")

    assert record_seen_withdrawal(db, account_id, record) is True
    assert record_seen_withdrawal(db, account_id, dict(record)) is False


def test_known_address_roundtrip(db):
    account_id = make_account(db)

    assert is_known_address(db, account_id, "bc1qabc") is False
    record_known_address(db, account_id, "bc1qabc", "BTC", "deposit_history")
    assert is_known_address(db, account_id, "bc1qabc") is True


def test_latest_balance_snapshot_returns_newest_and_prune_keeps_it(db):
    account_id = make_account(db)
    record_balance_snapshot(db, account_id, {"USDT": 100})
    record_balance_snapshot(db, account_id, {"USDT": 50, "BTC": 1})

    assert latest_balance_snapshot(db, account_id) == {"USDT": Decimal("50"), "BTC": Decimal("1")}

    # Backdate a stale row past the retention window; prune must remove only it.
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_balance_snapshots (account_id, coin, quantity, snapshot_at) VALUES (%s, 'ETH', '9', %s)",
            (account_id, old),
        )
    assert prune_balance_snapshots(db, account_id, keep_days=7) == 1
    assert latest_balance_snapshot(db, account_id) == {"USDT": Decimal("50"), "BTC": Decimal("1")}


def test_seed_baseline_seeds_pre_existing_transfer_routes(db):
    # Regression: routes the customer already used must be baseline, not
    # first-poll HIGH transfer_route_never_seen alerts (an April spot->p2p
    # transfer alerted in July on live Bitget accounts before this seed).
    account_id = make_account(db)
    client = MagicMock()
    client.fetch_balance.return_value = {"total": {"USDT": 100}}
    client.fetch_deposits.return_value = []
    client.fetch_withdrawals.return_value = []
    client.fetch_closed_orders.return_value = []
    client.fetch_transfers.return_value = [
        {"id": "t-1", "currency": "USDT", "amount": 216.0, "fromAccount": "spot", "toAccount": "p2p"}
    ]
    client.fetch_account_info.return_value = {"data": {"authorities": "readonly_r", "ips": ""}}

    seed_baseline(db, account_id, client)

    assert "spot->p2p" in known_transfer_routes(db, account_id)


def test_snapshot_quantities_read_back_without_the_columns_padding(db):
    """numeric(38,18) hands back Decimal('1.000000000000000000') and
    Decimal('0E-18'). Those were the numbers the quantity-drop detector put in
    its alert text: a drop read "0.500000000000000000" and nothing explained
    read "0E-18"."""
    account_id = make_account(db)
    record_balance_snapshot(db, account_id, {"BTC": "1", "DOGE": "0.1", "DUST": "0", "ETH": 10})
    with db.cursor() as cur:
        cur.execute("SELECT quantity FROM guardian_balance_snapshots WHERE account_id = %s AND coin = 'BTC'",
                    (account_id,))
        raw = cur.fetchone()["quantity"]
    assert str(raw) == "1.000000000000000000", "the premise: the column pads what it stores"

    balances = latest_balance_snapshot(db, account_id)

    assert {coin: str(q) for coin, q in balances.items()} == {"BTC": "1", "DOGE": "0.1", "DUST": "0", "ETH": "10"}
    assert balances == {"BTC": Decimal("1"), "DOGE": Decimal("0.1"), "DUST": Decimal("0"), "ETH": Decimal("10")}


def test_a_quantity_drop_reads_plainly_and_detects_exactly_as_before(db):
    """The detector is untouched; it is handed cleaner numbers. Same verdict,
    same figures as from the padded values - only the spelling differs."""
    from guardian.detectors.quantity_drop import detect_quantity_drop
    from guardian.numeric import to_decimal

    account_id = make_account(db)
    record_balance_snapshot(db, account_id, {"BTC": "1"})
    previous = latest_balance_snapshot(db, account_id)
    with db.cursor() as cur:
        cur.execute("SELECT coin, quantity FROM guardian_balance_snapshots WHERE account_id = %s", (account_id,))
        padded = {r["coin"]: to_decimal(r["quantity"]) for r in cur.fetchall()}
    args = ({"BTC": Decimal("0.5")}, [], [], Decimal("0.5"), Decimal("0.00000001"))

    (detection,) = detect_quantity_drop(previous, *args)
    (from_padded,) = detect_quantity_drop(padded, *args)

    assert "dropped by 0.5 but only 0 is explained" in detection.message
    assert "(0.5 unaccounted for)" in detection.message
    assert "E-" not in detection.message and "0000000" not in detection.message
    assert detection.evidence["previous_quantity"] == "1" and detection.evidence["drop"] == "0.5"
    assert "0.500000000000000000" in from_padded.message, "the premise: the padded values read like this"
    for field in ("previous_quantity", "drop", "explained_outflow", "unexplained_amount"):
        assert Decimal(detection.evidence[field]) == Decimal(from_padded.evidence[field])
