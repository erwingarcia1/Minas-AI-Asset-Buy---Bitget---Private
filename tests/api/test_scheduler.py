from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from api.database import engine
from api.models import RecurringPurchase, Transaction
from api.services import purchase_service as ps_mod
from api.services.recurring_scheduler import (
    ADVISORY_LOCK_KEY, RecurringScheduler, add_months, next_run_after)
from tests.api.test_purchase_service import FakeBinance, FailingBinance


def dt(y, m, d):
    return datetime(y, m, d, 9, 30, tzinfo=timezone.utc)


def test_frequency_advancement():
    base = dt(2026, 9, 1)
    assert next_run_after("daily", base) == dt(2026, 9, 2)
    assert next_run_after("weekly", base) == dt(2026, 9, 8)
    assert next_run_after("biweekly", base) == dt(2026, 9, 15)
    assert next_run_after("monthly", base) == dt(2026, 10, 1)
    with pytest.raises(ValueError):
        next_run_after("fortnightly", base)


def test_month_end_edges():
    assert add_months(dt(2026, 1, 31)) == dt(2026, 2, 28)      # Jan 31 -> Feb 28
    assert add_months(dt(2028, 1, 31)) == dt(2028, 2, 29)      # leap year
    assert add_months(dt(2026, 8, 31)) == dt(2026, 9, 30)      # 31 -> 30-day month
    assert add_months(dt(2026, 12, 15)) == dt(2027, 1, 15)     # year rollover


async def test_tick_executes_due_and_advances(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    original_due = purchase.next_purchase_date
    n = await RecurringScheduler(dry_run=True).tick()
    assert n == 1
    await db.refresh(purchase)
    assert purchase.next_purchase_date > datetime.now(timezone.utc)
    assert purchase.next_purchase_date == original_due + timedelta(days=7)  # weekly
    tx = (await db.execute(select(Transaction).where(
        Transaction.recurring_purchase_id == purchase.id))).scalars().one()
    assert tx.status == "completed"


async def test_tick_ignores_not_due_and_disabled(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    purchase.next_purchase_date = datetime.now(timezone.utc) + timedelta(days=1)
    await db.commit()
    assert await RecurringScheduler(dry_run=True).tick() == 0
    purchase.next_purchase_date = datetime.now(timezone.utc) - timedelta(minutes=1)
    purchase.enabled = False
    await db.commit()
    assert await RecurringScheduler(dry_run=True).tick() == 0


async def test_failed_purchase_retries_later_not_immediately(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FailingBinance)
    now = datetime.now(timezone.utc)
    assert await RecurringScheduler(dry_run=True).tick() == 1
    await db.refresh(purchase)
    # retried in ~1h, so the next 60s tick will NOT hammer Binance
    assert purchase.next_purchase_date > now + timedelta(minutes=50)
    assert purchase.next_purchase_date < now + timedelta(minutes=70)
    tx = (await db.execute(select(Transaction).where(
        Transaction.recurring_purchase_id == purchase.id))).scalars().one()
    assert tx.status == "failed"


async def test_missed_intervals_do_not_cause_catchup_spree(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    purchase.next_purchase_date = datetime.now(timezone.utc) - timedelta(days=30)
    await db.commit()
    assert await RecurringScheduler(dry_run=True).tick() == 1
    txs = (await db.execute(select(Transaction).where(
        Transaction.recurring_purchase_id == purchase.id))).scalars().all()
    assert len(txs) == 1, "a month of downtime must yield ONE buy, not four"
    await db.refresh(purchase)
    assert purchase.next_purchase_date > datetime.now(timezone.utc)


async def test_advisory_lock_prevents_concurrent_ticks(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    async with engine.connect() as other:
        got = (await other.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})).scalar()
        assert got
        try:
            assert await RecurringScheduler(dry_run=True).tick() == 0
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            await other.commit()
