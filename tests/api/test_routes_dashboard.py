from datetime import timedelta

import pytest

from api.models import Purchase, RecurringPurchase, utcnow
from tests.api.conftest import auth_headers, make_autobuy_purchase, make_history


@pytest.fixture
async def rich_user(db, user, binance_key):
    btc = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key,
                                      status="completed", frequency="one_time", next_execution=None)
    # history: successful buys count, failed ones do not
    await make_history(db, user, purchase=btc, amount_crypto=0.001, cost_usd=100.0)
    await make_history(db, user, purchase=btc, amount_crypto=0.003, cost_usd=200.0)
    eth_hist = await make_autobuy_purchase(db, user, crypto="ETH", amount_usd=50.0, purchase_type="dca",
                                           key=binance_key, status="completed", next_execution=None)
    await make_history(db, user, purchase=eth_hist, amount_crypto=0.1, cost_usd=50.0)
    sol_hist = await make_autobuy_purchase(db, user, crypto="SOL", amount_usd=999.0, key=binance_key,
                                           status="failed", next_execution=None)
    await make_history(db, user, purchase=sol_hist, amount_crypto=1.0, cost_usd=999.0, status="failed")
    db.add_all([
        # Auto-Buy purchases (the dashboard's source of schedules): both types count
        Purchase(user_id=user.id, crypto_symbol="ETH", amount_usd=50, frequency="weekly",
                 purchase_type="autobuy", wallet_address="0x52908400098527886E0F7030069857D2E4169EE7",
                 blockchain_network="mainnet", next_execution=utcnow() + timedelta(days=3)),
        Purchase(user_id=user.id, crypto_symbol="SOL", amount_usd=25, frequency="monthly",
                 purchase_type="dca", status="paused", wallet_address="9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                 blockchain_network="mainnet", next_execution=utcnow() + timedelta(days=3)),
        Purchase(user_id=user.id, crypto_symbol="DOGE", amount_usd=15, frequency="monthly",
                 purchase_type="dca", wallet_address="DOGEwallet000000000000000000000000",
                 blockchain_network="mainnet", next_execution=utcnow() + timedelta(days=60)),
        # legacy schedule: must no longer surface on the dashboard
        RecurringPurchase(user_id=user.id, crypto="LTC", amount_usdt=40,
                          frequency="weekly", next_purchase_date=utcnow() + timedelta(days=1)),
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
    assert active == {"ETH", "DOGE"}, "paused purchases are not active; both autobuy and dca count"
    assert upcoming == {"ETH"}, "only purchases due within 30 days are upcoming"
    assert "LTC" not in active, "legacy recurring_purchases rows are ignored"
    types = {p["crypto"]: p["purchase_type"] for p in d["active_purchases"]}
    assert types == {"ETH": "autobuy", "DOGE": "dca"}
    assert {p["amount_usdt"] for p in d["active_purchases"]} == {50.0, 15.0}


async def test_quick_stats(client, rich_user):
    async with client as c:
        qs = (await c.get("/api/dashboard/stats", headers=auth_headers(rich_user))).json()
    assert qs["total_invested_usdt"] == 350.0
    assert qs["transaction_count"] == 4
    assert qs["active_purchases"] == 2, "active Auto-Buy purchases, not legacy schedules"
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
