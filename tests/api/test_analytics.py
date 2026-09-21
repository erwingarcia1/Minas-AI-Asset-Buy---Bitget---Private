import httpx
import pytest

import api.routes.analytics as an_mod
import api.services.market_data as md_mod
from api.models import Purchase, utcnow
from tests.api.conftest import auth_headers, make_autobuy_purchase, make_history

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
async def seeded(db, user, binance_key):
    # history (purchase_transactions): BTC 0.01 @ avg 50k -> +20% at 60k; ETH 0.1 @ 3000 -> -16.67% at 2500
    await make_history(db, user, crypto="BTC", amount_crypto=0.01, cost_usd=500.0, key=binance_key,
                       purchase=await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=500.0, key=binance_key,
                                                            status="completed", next_execution=None))
    await make_history(db, user, crypto="ETH", amount_crypto=0.1, cost_usd=300.0, key=binance_key,
                       purchase=await make_autobuy_purchase(db, user, crypto="ETH", amount_usd=300.0, key=binance_key,
                                                            status="completed", next_execution=None))
    await make_history(db, user, crypto="SOL", amount_crypto=5, cost_usd=400.0, status="failed",  # must not count
                       purchase=await make_autobuy_purchase(db, user, crypto="SOL", amount_usd=400.0, key=binance_key,
                                                            status="failed", next_execution=None))
    db.add_all([
        # Auto-Buy purchases drive the spend projection (both types)
        Purchase(user_id=user.id, crypto_symbol="BTC", amount_usd=100, frequency="weekly",
                 purchase_type="autobuy", wallet_address="bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
                 blockchain_network="mainnet", next_execution=utcnow()),
        Purchase(user_id=user.id, crypto_symbol="ETH", amount_usd=50, frequency="daily",
                 purchase_type="dca", wallet_address="0x52908400098527886E0F7030069857D2E4169EE7",
                 blockchain_network="mainnet", next_execution=utcnow()),
        Purchase(user_id=user.id, crypto_symbol="DOGE", amount_usd=25, frequency="monthly",
                 purchase_type="dca", status="paused", wallet_address="DOGEwallet000000000000000000000000",
                 blockchain_network="mainnet", next_execution=utcnow()),  # paused: excluded
        Purchase(user_id=user.id, crypto_symbol="SOL", amount_usd=500, frequency="one_time",
                 purchase_type="autobuy", wallet_address="9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                 blockchain_network="mainnet", next_execution=utcnow()),  # one-time: 0 / month
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
    assert "best_performer" not in p and "worst_performer" not in p, "no ranking: every percentage is in the table"
    assert by["BTC"]["price_precision"] == 2


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


async def test_comparison_and_share_of_invested(seeded, market):
    async with client() as c:
        comp = (await c.get("/api/analytics/comparison", headers=auth_headers(seeded))).json()
    by = {e["crypto"]: e for e in comp["entries"]}
    # bought BTC at 50k vs 60k market -> -16.67% (below market = good)
    assert by["BTC"]["buy_price_vs_market_pct"] == pytest.approx(-16.67, abs=0.01)
    # 500/800 and 300/800 of the COST invested: the shares are reported, no score is derived
    assert "diversification_score" not in comp
    assert comp["share_of_invested"] == [{"crypto": "BTC", "share_pct": 62.5}, {"crypto": "ETH", "share_pct": 37.5}]


async def test_recurring_projection(seeded, market):
    async with client() as c:
        r = (await c.get("/api/analytics/recurring", headers=auth_headers(seeded))).json()
    assert {s["crypto"] for s in r["schedules"]} == {"BTC", "ETH", "SOL"}  # paused excluded
    assert {s["purchase_type"] for s in r["schedules"]} == {"autobuy", "dca"}
    by = {s["crypto"]: s for s in r["schedules"]}
    assert by["SOL"]["monthly_usdt"] == 0.0, "one-time purchases add nothing to the recurring projection"
    assert r["expected_monthly_usdt"] == pytest.approx(100 * 4.35 + 50 * 30.44, abs=0.1)
    assert r["projected_annual_usdt"] == pytest.approx(r["expected_monthly_usdt"] * 12, abs=0.1)
    assert r["note"].startswith("Estimates only, not financial advice")


async def test_volatility_is_the_metric_only(seeded, market):
    async with client() as c:
        v = (await c.get("/api/analytics/volatility", headers=auth_headers(seeded))).json()
    by = {e["crypto"]: e for e in v["entries"]}
    assert by["BTC"]["level"] == "moderate" and by["BTC"]["summary"] == f"30-day volatility {by['BTC']['volatility_30d_pct']}% (moderate)."
    assert by["ETH"]["level"] == "high" and by["ETH"]["summary"].startswith("30-day volatility")
    for e in v["entries"]:
        assert "recommendation" not in e
        for word in ("fine", "cadence", "should", "would", "smaller", "larger", "frequent"):
            assert word not in e["summary"].lower(), f"guidance word {word!r} in {e['summary']!r}"


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


def test_price_precision_follows_the_pair():
    """Bitget quotes BTCUSDT to 2 decimals, XLMUSDT to 5 (live 2026-09-21); unknown pairs fall back to 8."""
    from api.services import bitget_market as bm
    assert bm.price_precision("BTC") == 2
    assert bm.fmt_price(76132.62, "BTC") == "76132.62"
    assert bm.price_precision("DOGE") == 8 and bm.fmt_price(0.2, "DOGE") == "0.20000000"
    assert bm.fmt_price(None, "BTC") == "market"
