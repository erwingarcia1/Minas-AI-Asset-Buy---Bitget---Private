import httpx
import pytest

import api.routes.analytics as an_mod
import api.services.market_data as md_mod
from api.models import RecurringPurchase, Transaction, utcnow
from tests.api.conftest import auth_headers

PRICES = {"BTC": 60000.0, "ETH": 2500.0}
VOLS = {"BTC": 2.5, "ETH": 6.1}


class FakeMarket:
    def __init__(self, prices=PRICES, vols=VOLS):
        self.prices, self.vols = prices, vols
    async def get_price(self, crypto):
        return self.prices.get(crypto.upper())
    async def get_volatility_30d(self, crypto):
        return self.vols.get(crypto.upper())


@pytest.fixture
def market(monkeypatch):
    fake = FakeMarket()
    monkeypatch.setattr(md_mod, "market_data", fake)
    return fake


@pytest.fixture
async def seeded(db, user):
    db.add_all([
        # BTC: 0.01 @ avg 50k -> +20% at 60k
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.01,
                    cost_usdt=500.0, status="completed"),
        # ETH: 0.1 @ avg 3000 -> -16.67% at 2500
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.1,
                    cost_usdt=300.0, status="completed"),
        Transaction(user_id=user.id, crypto="SOL", amount_crypto=5,
                    cost_usdt=400.0, status="failed"),  # must not count
        RecurringPurchase(user_id=user.id, crypto="BTC", amount_usdt=100,
                          frequency="weekly", next_purchase_date=utcnow()),
        RecurringPurchase(user_id=user.id, crypto="ETH", amount_usdt=50,
                          frequency="daily", next_purchase_date=utcnow()),
        RecurringPurchase(user_id=user.id, crypto="DOGE", amount_usdt=25,
                          frequency="monthly", enabled=False,
                          next_purchase_date=utcnow()),  # disabled: excluded
    ])
    await db.commit()
    return user


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_performance_math(seeded, market):
    async with client() as c:
        p = (await c.get("/api/analytics/performance", headers=auth_headers(seeded))).json()
    assert p["price_available"] is True
    by = {h["crypto"]: h for h in p["holdings"]}
    assert by["BTC"]["average_buy_price"] == 50000.0
    assert by["BTC"]["market_value"] == 600.0
    assert by["BTC"]["unrealized_pnl_pct"] == 20.0
    assert by["ETH"]["unrealized_pnl_pct"] == pytest.approx(-16.67, abs=0.01)
    assert "SOL" not in by
    assert p["total_invested_usdt"] == 800.0
    assert p["total_market_value"] == 850.0
    assert p["best_performer"] == "BTC" and p["worst_performer"] == "ETH"


async def test_performance_degrades_without_prices(seeded, monkeypatch):
    monkeypatch.setattr(md_mod, "market_data", FakeMarket(prices={}, vols={}))
    async with client() as c:
        p = (await c.get("/api/analytics/performance", headers=auth_headers(seeded))).json()
    assert p["price_available"] is False
    assert p["total_invested_usdt"] == 800.0
    assert p["total_market_value"] is None
    assert all(h["current_price"] is None for h in p["holdings"])


async def test_performance_empty_user(db, user, market):
    async with client() as c:
        p = (await c.get("/api/analytics/performance", headers=auth_headers(user))).json()
    assert p["holdings"] == [] and p["total_invested_usdt"] == 0.0


async def test_comparison_and_diversification(seeded, market):
    async with client() as c:
        comp = (await c.get("/api/analytics/comparison", headers=auth_headers(seeded))).json()
    by = {e["crypto"]: e for e in comp["entries"]}
    # bought BTC at 50k vs 60k market -> -16.67% (below market = good)
    assert by["BTC"]["buy_price_vs_market_pct"] == pytest.approx(-16.67, abs=0.01)
    # 500/800 and 300/800 split -> 1 - (0.625^2 + 0.375^2) = 0.4688
    assert comp["diversification_score"] == pytest.approx(46.9, abs=0.1)


async def test_recurring_projection(seeded, market):
    async with client() as c:
        r = (await c.get("/api/analytics/recurring", headers=auth_headers(seeded))).json()
    assert {s["crypto"] for s in r["schedules"]} == {"BTC", "ETH"}  # disabled excluded
    assert r["expected_monthly_usdt"] == pytest.approx(100 * 4.35 + 50 * 30.44, abs=0.1)
    assert r["projected_annual_usdt"] == pytest.approx(r["expected_monthly_usdt"] * 12, abs=0.1)


async def test_volatility_recommendations(seeded, market):
    async with client() as c:
        v = (await c.get("/api/analytics/volatility", headers=auth_headers(seeded))).json()
    by = {e["crypto"]: e for e in v["entries"]}
    assert by["BTC"]["level"] == "moderate"
    assert by["ETH"]["level"] == "high" and "more frequent" in by["ETH"]["recommendation"]


async def test_market_data_cache_and_degradation(monkeypatch):
    calls = {"n": 0}
    class FakeLegacy:
        def get_ticker_price(self, symbol):
            calls["n"] += 1
            return 42.0
    md = md_mod.MarketData(client=FakeLegacy())
    from api.services.redis_client import get_redis
    await get_redis().delete("market:price:XRPUSDT")
    assert await md.get_price("XRP") == 42.0
    assert await md.get_price("XRP") == 42.0
    assert calls["n"] == 1, "second read must come from cache"
    class BrokenLegacy:
        def get_ticker_price(self, symbol):
            raise RuntimeError("down")
    md2 = md_mod.MarketData(client=BrokenLegacy())
    await get_redis().delete("market:price:NOPEUSDT")
    assert await md2.get_price("NOPE") is None
