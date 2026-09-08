"""Auto-Buy scheduler: due-purchase discovery, execution, frequency advancement,
transaction records, KYC gating and retry/failure handling."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import PurchaseTransaction
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import (BTC_MAIN, binance_key, client, db, make_key, make_purchase,  # noqa: F401
                                    other_user, user)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


class FakeGateway:
    def __init__(self):
        self.calls = []

    def execute(self, account, purchase, dry_run):
        self.calls.append((account.id, purchase.id, dry_run))
        return {"exchange_tx_id": f"wd-{purchase.id.hex[:8]}-{len(self.calls)}",
                "amount_executed": purchase.amount_usd / 50_000, "fee": 0.00005}


class FailingGateway(FakeGateway):
    def execute(self, account, purchase, dry_run):
        self.calls.append((account.id, purchase.id, dry_run))
        raise ps.ExecutionError("WITHDRAW_UNSUPPORTED: exchange said no")


async def tx_for(db, purchase):
    return (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == purchase.id)
                             .order_by(PurchaseTransaction.created_at))).scalars().all()


# --------------------------------------------------------------------------- discovery

async def test_finds_only_active_due_purchases(db, user, binance_key):
    due = await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(minutes=1))
    exactly = await make_purchase(db, user, binance_key, next_execution=NOW)
    future = await make_purchase(db, user, binance_key, next_execution=NOW + timedelta(minutes=1))
    paused = await make_purchase(db, user, binance_key, status="paused", next_execution=NOW - timedelta(hours=1))
    cancelled = await make_purchase(db, user, binance_key, status="cancelled", next_execution=None)
    found = {p.id for p in await ps.find_due_purchases(db, NOW)}
    assert due.id in found and exactly.id in found
    assert not ({future.id, paused.id, cancelled.id} & found)


# --------------------------------------------------------------------------- execution

@pytest.mark.parametrize("frequency, expected_next", [
    ("daily", NOW + timedelta(days=1)),
    ("weekly", NOW + timedelta(days=7)),
    ("monthly", datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)),
])
async def test_success_updates_next_execution_by_frequency(db, user, binance_key, frequency, expected_next):
    p = await make_purchase(db, user, binance_key, frequency=frequency, next_execution=NOW - timedelta(minutes=5))
    gw = FakeGateway()
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert counts["executed"] >= 1
    await db.refresh(p)
    assert p.next_execution == expected_next and p.status == "active" and p.failure_count == 0
    assert gw.calls and gw.calls[0][2] is True, "dry_run flag reaches the gateway"


async def test_one_time_purchase_completes(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, frequency="one_time", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    await db.refresh(p)
    assert p.status == "completed" and p.next_execution is None


async def test_creates_transaction_record(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    txs = await tx_for(db, p)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.status == "success" and tx.exchange_tx_id.startswith("wd-")
    assert tx.amount_executed == pytest.approx(100 / 50_000) and tx.fee == 0.00005
    assert tx.executed_at == NOW and tx.error_message is None


async def test_month_end_clamping():
    jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc)
    assert ps.add_frequency(jan31, "monthly") == datetime(2026, 2, 28, 9, 0, tzinfo=timezone.utc)
    dec = datetime(2026, 12, 15, 9, 0, tzinfo=timezone.utc)
    assert ps.add_frequency(dec, "monthly") == datetime(2027, 1, 15, 9, 0, tzinfo=timezone.utc)
    assert ps.add_frequency(jan31, "one_time") is None
    with pytest.raises(ValueError):
        ps.add_frequency(jan31, "hourly")


# --------------------------------------------------------------------------- gating

async def test_skips_unverified_or_suspended_users(db, user, other_user, binance_key):
    user.kyc_status = "pending"
    await db.commit()
    p = await make_purchase(db, user, binance_key, next_execution=NOW)
    other_key = await make_key(db, other_user)
    other_user.suspended = True
    await db.commit()
    q = await make_purchase(db, other_user, other_key, next_execution=NOW)
    gw = FakeGateway()
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert gw.calls == [], "no exchange call for ineligible users"
    assert counts["skipped"] >= 2
    assert await tx_for(db, p) == [] and await tx_for(db, q) == []
    await db.refresh(p)
    assert p.next_execution == NOW and p.status == "active", "left due; executes once the user is verified"


# --------------------------------------------------------------------------- failures

async def test_failure_records_attempts_and_marks_failed_after_three(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, next_execution=NOW)
    gw = FailingGateway()
    for attempt in (1, 2):
        counts = await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5 * attempt), gateway=gw, dry_run=True)
        assert counts["failed"] >= 1
        await db.refresh(p)
        assert p.status == "active" and p.failure_count == attempt
        assert p.next_execution == NOW, "stays due for the retry"
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=15), gateway=gw, dry_run=True)
    await db.refresh(p)
    assert p.status == "failed" and p.failure_count == 3 and p.next_execution is None
    assert "WITHDRAW_UNSUPPORTED" in p.last_error
    txs = await tx_for(db, p)
    assert [t.status for t in txs] == ["failed"] * 3 and all(t.error_message for t in txs)
    # a failed purchase is never picked up again
    assert p.id not in {x.id for x in await ps.find_due_purchases(db, NOW + timedelta(days=1))}


async def test_success_resets_failure_count(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, next_execution=NOW, failure_count=2, last_error="old")
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    await db.refresh(p)
    assert p.failure_count == 0 and p.last_error is None and p.status == "active"


async def test_missing_exchange_account_is_a_failure(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, next_execution=NOW)
    p.exchange_account_id = None
    await db.commit()
    gw = FakeGateway()
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert gw.calls == []
    txs = await tx_for(db, p)
    assert txs[0].status == "failed" and "exchange account" in txs[0].error_message


async def test_gateway_dry_run_via_real_client_is_simulated(db, user, binance_key, monkeypatch):
    """The production gateway prices, buys and withdraws through ExchangeClient; in dry-run nothing is sent."""
    from api.services import binance_client as bc

    class FakeLegacy:
        def __init__(self, *a, **k): pass
        @staticmethod
        def spot_symbol(b, q): return f"{b}{q}"
        def get_ticker_price(self, symbol): return 50_000.0
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "success" and tx.exchange_tx_id.startswith("DRY-WD-")
    assert tx.amount_executed == pytest.approx(0.002)


async def test_accumulate_mode_buys_without_withdrawing(db, user, binance_key, monkeypatch):
    """withdraw_immediately=False: the real gateway places the buy and never calls withdraw."""
    from api.services import binance_client as bc
    calls = []

    class FakeLegacy:
        def __init__(self, *a, **k): pass
        @staticmethod
        def spot_symbol(b, q): return f"{b}{q}"
        def get_ticker_price(self, symbol): return 2_000.0
        def withdraw(self, *a, **k): calls.append("withdraw"); return "should-not-happen"
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    p = await make_purchase(db, user, binance_key, crypto_symbol="ETH", wallet_address=None,
                            withdraw_immediately=False, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "success" and tx.exchange_tx_id.startswith("DRY-") and not tx.exchange_tx_id.startswith("DRY-WD-")
    assert tx.amount_executed == pytest.approx(0.05) and calls == []
    await db.refresh(p)
    assert p.next_execution == NOW + timedelta(days=1)


async def test_immediate_mode_without_wallet_fails_honestly(db, user, binance_key, monkeypatch):
    from api.services import binance_client as bc

    class FakeLegacy:
        def __init__(self, *a, **k): pass
        @staticmethod
        def spot_symbol(b, q): return f"{b}{q}"
        def get_ticker_price(self, symbol): return 50_000.0
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    p = await make_purchase(db, user, binance_key, wallet_address=None, withdraw_immediately=True, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "failed" and "wallet" in tx.error_message


async def test_sync_entry_point_runs(db, user, binance_key):
    """execute_purchases() (APScheduler target) runs the async core on its own loop and never raises."""
    import asyncio
    counts = await asyncio.to_thread(ps.execute_purchases)
    assert set(counts) == {"executed", "failed", "skipped"}
