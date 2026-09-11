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


class FakeLegacy:
    """Legacy exchange client stand-in: fixed price, records withdrawals."""
    withdrawals = []

    def __init__(self, *a, **k): pass
    @staticmethod
    def spot_symbol(b, q): return f"{b}{q}"
    def get_ticker_price(self, symbol): return 2_000.0 if symbol.startswith("ETH") else 50_000.0
    def withdraw(self, *a, **k): FakeLegacy.withdrawals.append((a, k)); return "real-wd-1"


async def test_autobuy_mode_withdraws_and_records_withdrawal_tx(db, user, binance_key, monkeypatch):
    from api.services import binance_client as bc
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    FakeLegacy.withdrawals.clear()
    p = await make_purchase(db, user, binance_key, purchase_type="autobuy", amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "success" and tx.tx_type == "withdrawal"
    assert tx.exchange_tx_id.startswith("DRY-WD-"), "dry run: withdrawal simulated, not sent"
    assert tx.amount_executed == pytest.approx(0.002) and FakeLegacy.withdrawals == []


async def test_dca_mode_skips_withdrawal_and_records_accumulation_tx(db, user, binance_key, monkeypatch):
    from api.services import binance_client as bc
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    FakeLegacy.withdrawals.clear()
    p = await make_purchase(db, user, binance_key, crypto_symbol="ETH", wallet_address="0x52908400098527886E0F7030069857D2E4169EE7",
                            purchase_type="dca", amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=False)  # live path, still no withdrawal
    tx = (await tx_for(db, p))[0]
    assert tx.status in ("success", "failed")
    if tx.status == "success":  # FakeLegacy has no order book, so the live buy path may fail before recording
        assert tx.tx_type == "accumulation"
    assert FakeLegacy.withdrawals == [], "dca never withdraws"
    # dry-run path records the accumulation cleanly
    p2 = await make_purchase(db, user, binance_key, crypto_symbol="ETH", wallet_address="0x52908400098527886E0F7030069857D2E4169EE7",
                             purchase_type="dca", amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=ps.ExchangeClientGateway(), dry_run=True)
    tx2 = (await tx_for(db, p2))[0]
    assert tx2.status == "success" and tx2.tx_type == "accumulation"
    assert tx2.exchange_tx_id.startswith("DRY-") and not tx2.exchange_tx_id.startswith("DRY-WD-")
    assert tx2.amount_executed == pytest.approx(0.05)
    await db.refresh(p2)
    assert p2.next_execution == NOW + timedelta(days=1)


async def test_failed_attempt_keeps_mode_tx_type(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    tx = (await tx_for(db, p))[0]
    assert tx.status == "failed" and tx.tx_type == "accumulation"


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
    def execute(self, account, purchase, dry_run):
        import time as _t
        _t.sleep(0.6)
        return super().execute(account, purchase, dry_run)


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
    assert len(gw.calls) == 2, "each purchase executed exactly once"
    assert len(await tx_for(db, p1)) == 1 and len(await tx_for(db, p2)) == 1
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
