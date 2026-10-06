"""Auto-Buy scheduler: due-purchase discovery, execution, frequency advancement,
transaction records, KYC gating and retry/failure handling."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import PurchaseTransaction
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import (bitget_key,  # noqa: F401
                                    BTC_MAIN, binance_key, client, db, make_key, make_purchase,  # noqa: F401
                                    other_user, user)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


from tests.fake_gateway import BuyFails, FailingGateway, FakeGateway, RichGateway  # noqa: F401


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
    ("daily", (NOW - timedelta(minutes=5)) + timedelta(days=1)),
    ("weekly", (NOW - timedelta(minutes=5)) + timedelta(days=7)),
    ("monthly", datetime(2026, 10, 8, 11, 55, tzinfo=timezone.utc)),   # the due time's day and clock, next month
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


async def test_creates_buy_and_withdrawal_rows(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    gw = FakeGateway()
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    txs = await tx_for(db, p)
    assert [t.tx_type for t in txs] == ["buy", "withdrawal"]
    buy, wd = txs
    assert buy.status == "success" and buy.order_id.startswith("ORD-1-") and buy.exchange_tx_id == buy.order_id
    assert buy.client_oid.startswith("ab-") and gw.placed[0][0] == buy.client_oid, "client id stored before sending"
    assert buy.amount_executed == pytest.approx(100 / 50_000) and buy.fill_price == 50_000.0 and buy.cost_usd == 100
    assert buy.fee == pytest.approx(100 / 50_000 * 0.001) and buy.fee_currency == "BTC"
    assert buy.executed_at == NOW and buy.error_message is None
    assert wd.status == "success" and wd.parent_transaction_id == buy.id and wd.withdrawal_id.startswith("WD-1-")
    assert wd.amount_executed == pytest.approx(100 / 50_000 * 0.999), "buy fee in the base coin is deducted"
    assert wd.withdrawal_fee == 0.0001 and wd.withdrawal_fee_currency == "BTC" and wd.client_oid.startswith("abw-")


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

async def test_failure_records_attempts_and_misses_the_cycle_after_three(db, user, binance_key):
    """Three attempts one tick apart; then a recurring purchase moves on to its next
    interval (it never stalls) and reports the missed cycle."""
    p = await make_purchase(db, user, binance_key, next_execution=NOW)   # daily
    gw = BuyFails()
    for attempt in (1, 2):
        counts = await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5 * attempt), gateway=gw, dry_run=True)
        assert counts["failed"] >= 1
        await db.refresh(p)
        assert p.status == "active" and p.failure_count == attempt
        assert p.next_execution == NOW, "stays due for the retry"
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=15), gateway=gw, dry_run=True)
    await db.refresh(p)
    assert p.status == "active" and p.failure_count == 3
    assert p.next_execution == NOW + timedelta(days=1), "the missed cycle's successor on the anchor grid (due time + a day), not stalled"
    assert "missed this cycle (1 of 3 in a row)" in p.last_error and "MIN_NOTIONAL" in p.last_error
    txs = await tx_for(db, p)
    assert [t.status for t in txs] == ["failed"] * 3 and all(t.error_message for t in txs)
    assert all(t.tx_type == "buy" and t.client_oid for t in txs)
    # the missed cycle is not retried early; the purchase is due again exactly on its next grid point
    assert p.id not in {x.id for x in await ps.find_due_purchases(db, NOW + timedelta(days=1) - timedelta(seconds=1))}
    assert p.id in {x.id for x in await ps.find_due_purchases(db, NOW + timedelta(days=1))}


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
    assert txs[0].status == "failed" and txs[0].error_message == ps.KEY_REMOVED_MESSAGE
    await db.refresh(p)
    assert p.status == "failed" and p.next_execution is None, "stopped at once, no three retries"


async def test_gateway_dry_run_via_real_client_is_simulated(db, user, binance_key, monkeypatch):
    """The production gateway prices, buys and withdraws through ExchangeClient; in dry-run nothing is sent."""
    from api.services import exchange_client as bc

    class FakeLegacy:
        def __init__(self, *a, **k): pass
        @staticmethod
        def spot_symbol(b, q): return f"{b}{q}"
        def get_ticker_price(self, symbol): return 50_000.0
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    buy, wd = await tx_for(db, p)
    assert buy.status == "success" and buy.order_id.startswith("DRY-ab-") and buy.amount_executed == pytest.approx(0.002)
    assert wd.status == "success" and wd.exchange_tx_id.startswith("DRY-WD-") and wd.parent_transaction_id == buy.id


class FakeLegacy:
    """Legacy exchange client stand-in: fixed price, records withdrawals."""
    withdrawals = []

    def __init__(self, *a, **k): pass
    @staticmethod
    def spot_symbol(b, q): return f"{b}{q}"
    def get_ticker_price(self, symbol): return 2_000.0 if symbol.startswith("ETH") else 50_000.0
    def withdraw(self, *a, **k): FakeLegacy.withdrawals.append((a, k)); return "real-wd-1"


async def test_autobuy_mode_withdraws_and_records_withdrawal_tx(db, user, binance_key, monkeypatch):
    from api.services import exchange_client as bc
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    FakeLegacy.withdrawals.clear()
    p = await make_purchase(db, user, binance_key, purchase_type="autobuy", amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    buy, wd = await tx_for(db, p)
    assert buy.status == "success" and buy.tx_type == "buy" and buy.order_id.startswith("DRY-ab-")
    assert buy.amount_executed == pytest.approx(0.002)
    assert wd.status == "success" and wd.tx_type == "withdrawal" and wd.parent_transaction_id == buy.id
    assert wd.withdrawal_id.startswith("DRY-WD-"), "dry run: withdrawal simulated, not sent"
    assert FakeLegacy.withdrawals == []


async def test_dca_mode_skips_withdrawal_and_records_a_buy_row(db, user, binance_key, monkeypatch):
    from api.services import exchange_client as bc
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    FakeLegacy.withdrawals.clear()
    p2 = await make_purchase(db, user, binance_key, crypto_symbol="ETH", wallet_address="0x52908400098527886E0F7030069857D2E4169EE7",
                             purchase_type="dca", amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    txs = await tx_for(db, p2)
    assert [t.tx_type for t in txs] == ["buy"], "dca never withdraws"
    assert txs[0].status == "success" and txs[0].order_id.startswith("DRY-") and txs[0].amount_executed == pytest.approx(0.05)
    assert FakeLegacy.withdrawals == []
    await db.refresh(p2)
    assert p2.next_execution == NOW + timedelta(days=1)


async def test_failed_buy_attempt_is_a_buy_row(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "failed" and tx.tx_type == "buy" and tx.client_oid


async def test_sync_entry_point_runs(db, user, binance_key):
    """execute_purchases() (APScheduler target) runs the async core on its own loop and never raises."""
    import asyncio
    counts = await asyncio.to_thread(ps.execute_purchases)
    assert set(counts) == {"executed", "failed", "skipped"}


# --------------------------------------------------------------------------- observability

async def test_run_logs_start_and_summary_even_when_nothing_is_due(db, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="autobuy.tasks.purchases")
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)   # nothing can be due before it existed
    counts = await ps.execute_due_purchases(db, now=far_past, gateway=FakeGateway(), dry_run=True)
    assert counts == {"executed": 0, "failed": 0, "skipped": 0}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("auto-buy run started" in m for m in msgs)
    assert any("0 due purchase(s) found" in m for m in msgs)
    assert any("auto-buy run finished" in m and "'executed': 0" in m for m in msgs), "summary must log even when idle"


async def test_sync_entry_point_logs_that_it_was_called(db, caplog):
    import asyncio, logging
    caplog.set_level(logging.INFO, logger="autobuy.tasks.purchases")
    await asyncio.to_thread(ps.execute_purchases)
    assert any("execute_purchases called by APScheduler" in r.getMessage() for r in caplog.records)


def test_job_event_listener_logs_missed_skipped_and_errors(caplog):
    import logging
    from types import SimpleNamespace
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
    caplog.set_level(logging.INFO, logger="autobuy.tasks.purchases")
    ev = lambda code, **k: SimpleNamespace(**{"code": code, "job_id": "execute_purchases", "scheduled_run_time": NOW, "exception": None, **k})
    ps.job_event_listener(ev(EVENT_JOB_MISSED))
    ps.job_event_listener(ev(EVENT_JOB_MAX_INSTANCES))
    ps.job_event_listener(ev(EVENT_JOB_ERROR, exception=RuntimeError("boom")))
    ps.job_event_listener(SimpleNamespace(code=EVENT_JOB_MISSED, job_id="other_job", scheduled_run_time=NOW))
    ps.job_event_listener(ev(EVENT_JOB_EXECUTED))  # not registered for this; must stay silent
    levels = [(r.levelname, r.getMessage()) for r in caplog.records if "execute_purchases" in r.getMessage()]
    assert [l for l, _ in levels] == ["WARNING", "WARNING", "ERROR"]
    assert "DISCARDED" in levels[0][1] and "SKIPPED" in levels[1][1] and "boom" in levels[2][1]


# --------------------------------------------------------------------------- single-instance safety + grace

class SlowGateway(FakeGateway):
    """Holds each execution long enough for a concurrent run to collide."""
    def place_buy(self, account, purchase, client_oid, dry_run):
        import time as _t
        _t.sleep(0.6)
        return super().place_buy(account, purchase, client_oid, dry_run)


async def test_concurrent_runs_execute_each_purchase_exactly_once(db, user, binance_key, caplog):
    """Two runs on two connections (as two gunicorn workers would be): one takes the
    advisory lock and executes; the other returns immediately with nothing done."""
    import asyncio, logging
    from api.database import AsyncSessionLocal
    caplog.set_level(logging.INFO, logger="autobuy.tasks.purchases")
    p1 = await make_purchase(db, user, binance_key, next_execution=NOW)
    p2 = await make_purchase(db, user, binance_key, crypto_symbol="ETH", wallet_address="0x52908400098527886E0F7030069857D2E4169EE7", next_execution=NOW)
    gw = SlowGateway()
    async with AsyncSessionLocal() as s1, AsyncSessionLocal() as s2:
        r1, r2 = await asyncio.gather(ps.execute_due_purchases(s1, now=NOW, gateway=gw, dry_run=True),
                                      ps.execute_due_purchases(s2, now=NOW, gateway=gw, dry_run=True))
    assert sorted([r1["executed"], r2["executed"]]) == [0, 2], (r1, r2)
    assert len(gw.placed) == 2, "each purchase bought exactly once"
    assert len(await tx_for(db, p1)) == 2 and len(await tx_for(db, p2)) == 2   # buy + withdrawal rows
    assert any("another worker holds the run lock" in r.getMessage() for r in caplog.records)


async def test_lock_is_released_after_a_run(db, user, binance_key):
    from sqlalchemy import text
    await make_purchase(db, user, binance_key, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    held = (await db.execute(text("select count(*) from pg_locks where locktype='advisory' and objid=:k"),
                             {"k": ps.ADVISORY_LOCK_KEY})).scalar()
    assert held == 0


def test_build_scheduler_sets_grace_and_listener():
    from apscheduler.events import EVENT_JOB_MISSED
    sched = ps.build_scheduler()
    job = sched.get_job("execute_purchases")
    assert job is not None and job.misfire_grace_time == 120 and job.max_instances == 1 and job.coalesce
    assert job.trigger.interval.total_seconds() == 300
    assert any(cb is ps.job_event_listener and (mask & EVENT_JOB_MISSED) for cb, mask in sched._listeners)


async def test_dry_run_prices_at_live_best_ask_with_no_rounding(db, user, bitget_key, monkeypatch, caplog):
    """Bitget dry runs are priced at the live best ask (the Bitget transport is patched; audit C1
    removed the duck-typed fallback that let a Binance-selected client answer this path)."""
    import logging
    from api.services import exchange_client as bc

    class TickerClient:
        def __init__(self, *a, **k): pass
        @staticmethod
        def spot_symbol(b, q): return f"{b}{q}"
        def get_ticker(self, symbol): return {"last": 77042.68, "ask": 77042.84, "bid": 77042.83, "ts": "1789130047195"}
        def get_ticker_price(self, symbol): return 77042.68
    monkeypatch.setitem(bc.LEGACY_CLIENTS, "bitget", TickerClient)
    p = await make_purchase(db, user, bitget_key, amount_usd=15, purchase_type="dca", next_execution=NOW)
    with caplog.at_level(logging.INFO, logger="autobuy.exchange_client"):
        await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "success" and tx.fill_price == 77042.84, "best ask, unrounded"
    assert tx.amount_executed == pytest.approx(15 / 77042.84)
    assert any("dry-run priced BTCUSDT at ask 77042.84" in r.getMessage() for r in caplog.records)


async def test_dry_run_fails_closed_when_no_live_price(db, user, bitget_key, monkeypatch):
    from api.services import exchange_client as bc

    class NoPrice:
        def __init__(self, *a, **k): pass
        @staticmethod
        def spot_symbol(b, q): return f"{b}{q}"
        def get_ticker(self, symbol): return {"last": None, "ask": None, "bid": None, "ts": None}
    monkeypatch.setitem(bc.LEGACY_CLIENTS, "bitget", NoPrice)
    p = await make_purchase(db, user, bitget_key, amount_usd=15, purchase_type="dca", next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    assert counts["failed"] == 1
    tx = (await tx_for(db, p))[0]
    assert tx.status == "failed" and "PRICE_UNAVAILABLE" in tx.error_message and tx.amount_executed is None
