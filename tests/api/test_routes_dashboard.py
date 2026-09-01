from datetime import timedelta

import pytest

from api.models import RecurringPurchase, Transaction, utcnow
from tests.api.conftest import auth_headers


@pytest.fixture
async def rich_user(db, user):
    db.add_all([
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.001,
                    cost_usdt=100.0, status="completed"),
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.003,
                    cost_usdt=200.0, status="completed"),
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.1,
                    cost_usdt=50.0, status="completed"),
        Transaction(user_id=user.id, crypto="SOL", amount_crypto=1.0,
                    cost_usdt=999.0, status="failed"),
        RecurringPurchase(user_id=user.id, crypto="ETH", amount_usdt=50,
                          frequency="weekly", next_purchase_date=utcnow() + timedelta(days=3)),
        RecurringPurchase(user_id=user.id, crypto="SOL", amount_usdt=25,
                          frequency="monthly", enabled=False,
                          next_purchase_date=utcnow() + timedelta(days=3)),
        RecurringPurchase(user_id=user.id, crypto="DOGE", amount_usdt=15,
                          frequency="monthly", next_purchase_date=utcnow() + timedelta(days=60)),
    ])
    await db.commit()
    return user


async def test_holdings_aggregation(client, rich_user):
    async with client as c:
        d = (await c.get("/api/dashboard/", headers=auth_headers(rich_user))).json()
    holdings = {h["crypto"]: h for h in d["holdings"]}
    assert holdings["BTC"]["total_amount"] == pytest.approx(0.004)
    assert holdings["BTC"]["total_cost_usdt"] == 300.0
    assert holdings["ETH"]["total_cost_usdt"] == 50.0
    assert "SOL" not in holdings, "failed transactions must not count as holdings"


async def test_active_vs_upcoming(client, rich_user):
    async with client as c:
        d = (await c.get("/api/dashboard/", headers=auth_headers(rich_user))).json()
    active = {p["crypto"] for p in d["active_purchases"]}
    upcoming = {p["crypto"] for p in d["upcoming_purchases"]}
    assert active == {"ETH", "DOGE"}, "disabled schedules are not active"
    assert upcoming == {"ETH"}, "only schedules due within 30 days are upcoming"


async def test_quick_stats(client, rich_user):
    async with client as c:
        qs = (await c.get("/api/dashboard/stats", headers=auth_headers(rich_user))).json()
    assert qs["total_invested_usdt"] == 350.0
    assert qs["transaction_count"] == 4
    assert qs["active_purchases"] == 2
    assert qs["kyc_status"] == "verified"


async def test_monthly_average(client, rich_user):
    async with client as c:
        d = (await c.get("/api/dashboard/", headers=auth_headers(rich_user))).json()
    assert d["monthly_average_usdt"] == 350.0  # single month of history


async def test_timeline_zero_fill_and_bounds(client, rich_user):
    H = auth_headers(rich_user)
    async with client as c:
        tl = (await c.get("/api/dashboard/analytics/timeline", params={"days": 5}, headers=H)).json()
        assert tl["days"] == 5 and len(tl["timeline"]) == 5
        assert tl["timeline"][-1]["total_usdt"] == 350.0
        assert all(p["total_usdt"] == 0.0 for p in tl["timeline"][:-1])
        assert (await c.get("/api/dashboard/analytics/timeline",
                            params={"days": 0}, headers=H)).status_code == 422
        assert (await c.get("/api/dashboard/analytics/timeline",
                            params={"days": 400}, headers=H)).status_code == 422
