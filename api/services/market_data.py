"""Public Binance market data with Redis caching.

Prices and volatility need no API keys (public endpoints). Prices cache for
60s, volatility for 1h. Every getter degrades to None on provider or cache
failure — analytics endpoints then answer cost-basis-only rather than 5xx.
"""
import asyncio
import json
import logging
import statistics

from binance_client import BinanceClient as _LegacyClient

from api.services.cache import cached_json
from api.services.metrics import time_exchange
from api.services.redis_client import get_redis

logger = logging.getLogger("autobuy.market")

PRICE_TTL_S = 60
VOLATILITY_TTL_S = 3600


class MarketData:
    def __init__(self, client=None):
        self._client = client or _LegacyClient()

    async def _cached(self, key: str, ttl: int, producer):
        """Degrade: a producer failure is logged and yields None; a failed cache write is silent."""
        def degrade(exc: Exception):
            logger.warning("market data fetch failed for %s: %s", key, exc)
            return None
        return await cached_json(key, ttl, producer, on_produce_error=degrade,
                                 read_warning="market cache read failed for %s", write_warning=None)

    async def get_price(self, crypto: str) -> float | None:
        """Current spot price in USDT, or None when unavailable."""
        symbol = f"{crypto.upper()}USDT"

        async def fetch():
            with time_exchange("public_price"):
                return await asyncio.to_thread(self._client.get_ticker_price, symbol)

        return await self._cached(f"market:price:{symbol}", PRICE_TTL_S, fetch)

    async def get_volatility_30d(self, crypto: str) -> float | None:
        """Stddev of daily returns over ~30 days, as a percentage. None if unavailable."""
        symbol = f"{crypto.upper()}USDT"

        async def fetch():
            with time_exchange("public_klines"):
                klines = await asyncio.to_thread(
                    self._client._request, "GET", "/api/v3/klines",
                    {"symbol": symbol, "interval": "1d", "limit": 31})
            closes = [float(k[4]) for k in klines]
            if len(closes) < 3:
                return None
            returns = [(b - a) / a for a, b in zip(closes, closes[1:])]
            return round(statistics.stdev(returns) * 100, 2)

        return await self._cached(f"market:vol:{symbol}", VOLATILITY_TTL_S, fetch)


market_data = MarketData()
