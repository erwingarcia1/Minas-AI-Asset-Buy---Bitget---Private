"""api/services/cache.py failure policy, exercised through its two real callers
(audit Batch B2 unified three cache copies; each caller must keep its policy)."""
import pytest

from api.services import bitget_market as bm
from api.services import market_data as md_mod
from tests.conftest_autobuy import bitget_key, client, db, make_key, user  # noqa: F401


class FakeRedis:
    """Async redis double: get/setex raise or behave as configured."""
    def __init__(self, read_raises=False, write_raises=False, stored=None):
        self.read_raises, self.write_raises, self.stored = read_raises, write_raises, stored
        self.writes = []

    async def get(self, key):
        if self.read_raises:
            raise ConnectionError("redis down")
        return self.stored

    async def setex(self, key, ttl, value):
        if self.write_raises:
            raise ConnectionError("redis down")
        self.writes.append((key, ttl, value))


class FakeSyncRedis(FakeRedis):
    def get(self, key):   # noqa: D102  (sync twin)
        if self.read_raises:
            raise ConnectionError("redis down")
        return self.stored

    def setex(self, key, ttl, value):
        if self.write_raises:
            raise ConnectionError("redis down")
        self.writes.append((key, ttl, value))

    def close(self):
        pass


@pytest.fixture
def real_bitget_cache(monkeypatch):
    """The autouse stub bypasses the cache; put the real functions back."""
    monkeypatch.setattr(bm, "_cached", bm.cached_real)
    monkeypatch.setattr(bm, "_cached_sync", bm.cached_sync_real)


# ---- bitget_market: fail closed --------------------------------------------------------

async def test_bitget_cache_read_failure_runs_the_producer(real_bitget_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(bm, "fetch_symbol", lambda symbol: calls.append(symbol) or {"symbol": symbol, "status": "online",
                        "minTradeUSDT": "1", "takerFeeRate": "0.002", "quantityPrecision": "6", "pricePrecision": "2"})
    r = FakeSyncRedis(read_raises=True)
    monkeypatch.setattr(bm, "_sync_redis", lambda: r)
    sym = bm.get_symbol_sync("BTC")
    assert calls == ["BTCUSDT"] and sym["symbol"] == "BTCUSDT"
    assert r.writes and r.writes[0][0] == "bitget:market:symbol:BTCUSDT", "the fresh value is still cached"
    ar = FakeRedis(read_raises=True)
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: ar)
    sym = await bm.get_symbol("BTC")
    assert calls == ["BTCUSDT", "BTCUSDT"] and sym["symbol"] == "BTCUSDT" and ar.writes


async def test_bitget_producer_failure_raises_market_data_unavailable(real_bitget_cache, monkeypatch):
    def boom(symbol):
        raise TimeoutError("bitget timed out")
    monkeypatch.setattr(bm, "fetch_symbol", boom)
    monkeypatch.setattr(bm, "_sync_redis", lambda: FakeSyncRedis())
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: FakeRedis())
    with pytest.raises(bm.MarketDataUnavailable):
        bm.get_symbol_sync("BTC")
    with pytest.raises(bm.MarketDataUnavailable):
        await bm.get_symbol("BTC")


async def test_bitget_producer_failure_refuses_the_purchase(real_bitget_cache, client, user, bitget_key, monkeypatch):
    """End to end: Bitget unreachable -> 503, nothing created (the fail-closed policy the purchase route relies on)."""
    from tests.conftest_autobuy import auth
    def boom(symbol):
        raise TimeoutError("bitget timed out")
    monkeypatch.setattr(bm, "fetch_symbol", boom)
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: FakeRedis())
    r = await client.post("/api/v2/purchases", json={"crypto_symbol": "BTC", "amount_usd": 20, "frequency": "daily",
                                                     "exchange_account_id": str(bitget_key.id), "purchase_type": "dca"},
                          headers=auth(user))
    assert r.status_code == 503 and r.json()["detail"]["error"] == "exchange_data_unavailable"


async def test_bitget_cache_write_failure_still_returns_the_value(real_bitget_cache, monkeypatch, caplog):
    monkeypatch.setattr(bm, "fetch_symbol", lambda symbol: {"symbol": symbol, "status": "online", "minTradeUSDT": "1",
                                                            "takerFeeRate": "0.002", "quantityPrecision": "6", "pricePrecision": "2"})
    monkeypatch.setattr(bm, "_sync_redis", lambda: FakeSyncRedis(write_raises=True))
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: FakeRedis(write_raises=True))
    assert bm.get_symbol_sync("BTC")["symbol"] == "BTCUSDT"
    assert (await bm.get_symbol("BTC"))["symbol"] == "BTCUSDT"
    assert "cache write failed" in caplog.text


async def test_bitget_cache_hit_skips_the_producer(real_bitget_cache, monkeypatch):
    def boom(symbol):
        raise AssertionError("producer must not run on a cache hit")
    monkeypatch.setattr(bm, "fetch_symbol", boom)
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: FakeRedis(stored='{"symbol": "BTCUSDT", "online": true}'))
    assert (await bm.get_symbol("BTC"))["online"] is True


# ---- market_data: degrade ---------------------------------------------------------------

class BrokenTicker:
    def get_ticker_price(self, symbol):
        raise RuntimeError("upstream down")


class GoodTicker:
    def __init__(self):
        self.calls = 0

    def get_ticker_price(self, symbol):
        self.calls += 1
        return 42.0


async def test_market_data_producer_failure_returns_none(monkeypatch, caplog):
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: FakeRedis())
    assert await md_mod.MarketData(client=BrokenTicker()).get_price("XRP") is None
    assert "market data fetch failed" in caplog.text


async def test_market_data_cache_read_failure_runs_the_producer_and_write_failure_is_silent(monkeypatch, caplog):
    r = FakeRedis(read_raises=True, write_raises=True)
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: r)
    ticker = GoodTicker()
    assert await md_mod.MarketData(client=ticker).get_price("XRP") == 42.0
    assert ticker.calls == 1
    assert "market cache read failed" in caplog.text and "write failed" not in caplog.text
