from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import api.services.market_data as md_mod
from api.models import RecurringPurchase, Transaction, utcnow
from api.services import purchase_service as ps_mod
from api.services.purchase_service import PurchaseService, PurchaseSkipped
from api.services.recurring_scheduler import RecurringScheduler
from tests.api.conftest import auth_headers
from tests.api.test_analytics import FakeMarket
from tests.api.test_purchase_service import FakeBinance


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def market(monkeypatch):
    fake = FakeMarket(prices={"BTC": 60000.0, "ETH": 2500.0}, vols={"BTC": 6.0, "ETH": 1.0})
    monkeypatch.setattr(md_mod, "market_data", fake)
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    return fake


async def test_limit_order_skips_above_limit_and_executes_below(db, user, binance_key, market):
    order = RecurringPurchase(
        user_id=user.id, crypto="BTC", amount_usdt=100, frequency="daily",
        purchase_type="limit_order", limit_price=55000.0,
        next_purchase_date=utcnow())
    db.add(order)
    await db.commit()

    result = await PurchaseService(db).execute_purchase(user.id, order.id)
    assert isinstance(result, PurchaseSkipped)
    assert "above limit" in result.reason

    market.prices["BTC"] = 54000.0
    tx = await PurchaseService(db).execute_purchase(user.id, order.id)
    assert tx.status == "completed"
    await db.refresh(order)
    assert order.enabled is False, "limit orders are one-shot"


async def test_limit_order_never_executes_blind(db, user, purchase, binance_key, market):
    purchase.purchase_type = "limit_order"
    purchase.limit_price = 55000.0
    await db.commit()
    market.prices = {}
    result = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert isinstance(result, PurchaseSkipped)
    assert "price unavailable" in result.reason


async def test_dca_band_enforced_but_degrades_open(db, user, purchase, binance_key, market):
    purchase.min_price = 55000.0
    purchase.max_price = 65000.0
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"  # 60k inside band
    market.prices["BTC"] = 70000.0
    result = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert isinstance(result, PurchaseSkipped) and "ceiling" in result.reason
    market.prices = {}  # plain bands fail open: DCA proceeds without price data
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"


async def test_one_time_purchase_disables_after_execution(db, user, purchase, binance_key, market):
    purchase.purchase_type = "one_time"
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"
    await db.refresh(purchase)
    assert purchase.enabled is False


async def test_volatility_adaptive_halves_amount_and_interval(db, user, purchase, binance_key, market):
    purchase.volatility_enabled = True  # BTC vol mocked at 6% (high)
    original_due = purchase.next_purchase_date
    await db.commit()
    assert await RecurringScheduler(dry_run=True).tick() == 1
    tx = (await db.execute(select(Transaction).where(
        Transaction.recurring_purchase_id == purchase.id))).scalars().one()
    assert tx.cost_usdt == 50.0, "high volatility buys half the amount"
    await db.refresh(purchase)
    assert purchase.next_purchase_date == original_due + timedelta(days=3, hours=12), \
        "weekly interval halves to 3.5 days"


async def test_volatility_disabled_below_minimum(db, user, binance_key, market):
    small = RecurringPurchase(user_id=user.id, crypto="BTC", amount_usdt=15,
                              frequency="weekly", volatility_enabled=True,
                              next_purchase_date=utcnow())
    db.add(small)
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, small.id)
    assert tx.cost_usdt == 15.0, "half would violate the 10 USDT minimum"


async def test_scheduler_expires_limit_orders(db, user, binance_key, market):
    stale = RecurringPurchase(
        user_id=user.id, crypto="BTC", amount_usdt=100, frequency="daily",
        purchase_type="limit_order", limit_price=1.0,
        next_purchase_date=utcnow() - timedelta(hours=1),
        expires_at=utcnow() - timedelta(minutes=1))
    db.add(stale)
    await db.commit()
    assert await RecurringScheduler(dry_run=True).tick() == 1
    await db.refresh(stale)
    assert stale.enabled is False
    txs = (await db.execute(select(Transaction).where(
        Transaction.recurring_purchase_id == stale.id))).scalars().all()
    assert txs == [], "expired orders never execute"


async def test_skip_does_not_trip_failure_alert(db, user, purchase, binance_key, market):
    purchase.purchase_type = "limit_order"
    purchase.limit_price = 1.0  # unreachable -> always skipped
    await db.commit()
    sched = RecurringScheduler(dry_run=True)
    assert await sched.tick() == 1
    assert sched.consecutive_failures == 0, "a skip is not a failure"
    await db.refresh(purchase)
    assert purchase.next_purchase_date > datetime.now(timezone.utc), "re-checks later"


async def test_limit_order_route_and_rebalance_plan(db, user, market):
    H = auth_headers(user)
    async with client() as c:
        r = await c.post("/api/advanced/limit-order", headers=H,
                         json={"crypto": "btc", "amount_usdt": 100,
                               "limit_price": 50000, "expiry_days": 7})
        assert r.status_code == 201, r.text

        plan = await c.post("/api/advanced/rebalance", headers=H,
                            json={"target_allocations": {"BTC": 60, "ETH": 40},
                                  "amount_usdt": 100, "frequency": "weekly"})
        assert plan.status_code == 201, plan.text
        body = plan.json()
        assert body["total_per_period_usdt"] == 100
        assert {e["crypto"]: e["amount_usdt"] for e in body["entries"]} == \
               {"BTC": 60.0, "ETH": 40.0}

        dup = await c.post("/api/advanced/rebalance", headers=H,
                           json={"target_allocations": {"BTC": 100},
                                 "amount_usdt": 100, "frequency": "weekly"})
        assert dup.status_code == 422 and "already exists" in dup.text

        bad_sum = await c.post("/api/advanced/rebalance", headers=H,
                               json={"target_allocations": {"BTC": 60, "ETH": 30},
                                     "amount_usdt": 100, "frequency": "weekly"})
        assert bad_sum.status_code == 422

        tiny = await c.post("/api/advanced/rebalance", headers=H,
                            json={"target_allocations": {"BTC": 95, "ETH": 5},
                                  "amount_usdt": 100, "frequency": "weekly"})
        assert tiny.status_code == 422 and "minimum" in tiny.text


async def test_opportunities_detects_drift(db, user, market):
    H = auth_headers(user)
    db.add_all([
        RecurringPurchase(user_id=user.id, crypto="BTC", amount_usdt=60,
                          frequency="weekly", purchase_type="rebalancing",
                          target_allocation=60, next_purchase_date=utcnow()),
        RecurringPurchase(user_id=user.id, crypto="ETH", amount_usdt=40,
                          frequency="weekly", purchase_type="rebalancing",
                          target_allocation=40, next_purchase_date=utcnow()),
        # holdings heavily BTC: 0.01 BTC @60k = $600 vs 0.04 ETH @2.5k = $100
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.01,
                    cost_usdt=500, status="completed"),
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.04,
                    cost_usdt=120, status="completed"),
    ])
    await db.commit()
    async with client() as c:
        r = (await c.get("/api/advanced/opportunities", headers=H)).json()
    assert r["price_available"] is True
    kinds = {(o["crypto"], o["kind"]) for o in r["opportunities"]}
    assert ("ETH", "underweight") in kinds     # 14% actual vs 40% target
    assert ("BTC", "overweight") in kinds      # 86% actual vs 60% target
    eth = next(o for o in r["opportunities"]
               if o["crypto"] == "ETH" and o["kind"] == "underweight")
    assert eth["suggested_next_period_usdt"] == 100.0, \
        "entire next budget goes to the only underweight"
