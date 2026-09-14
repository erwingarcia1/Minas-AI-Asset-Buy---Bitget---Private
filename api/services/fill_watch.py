"""Fill deviation watch: fills inside the parking tolerance but outside the
warning band are still recorded as successful buys — the customer is not
blocked — but each one is logged with expected vs actual figures and counted
in Redis for 24 hours so a recurring few-percent shortfall raises an admin
alert instead of quietly leaking money at scale."""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("autobuy.fill_watch")

DAY_KEY = "autobuy:fill:deviations:{day}"     # counter per UTC day (kept 48 h)
RECENT_KEY = "autobuy:fill:deviations:recent"  # last entries for the admin view
RECENT_LENGTH = 100
WARN_ALERT_THRESHOLD = 3       # deviations in 24 h -> warning
CRITICAL_ALERT_THRESHOLD = 10  # -> critical


def _redis():
    from api.services.redis_client import get_redis
    return get_redis()


def _day(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))


async def record(purchase_id, user_id, coin: str, deviation_pct: float, expected_amount: float, actual_amount: float,
                 requested_usd: float, cost_usd: float, price: Optional[float], now: Optional[float] = None) -> None:
    """Log and count one deviation. Never raises."""
    logger.warning("fill deviation %.2f%%: purchase=%s %s expected %.8f got %.8f for %.2f USDT (requested %.2f) at %s",
                   deviation_pct, purchase_id, coin, expected_amount, actual_amount, cost_usd, requested_usd,
                   f"{price:.8g}" if price else "?")
    entry = {"purchase_id": str(purchase_id), "user_id": str(user_id), "coin": coin, "deviation_pct": round(deviation_pct, 3),
             "expected_amount": expected_amount, "actual_amount": actual_amount, "requested_usd": requested_usd,
             "cost_usd": cost_usd, "price": price, "at": now or time.time()}
    try:
        r = _redis()
        key = DAY_KEY.format(day=_day(now))
        await r.incr(key)
        await r.expire(key, 48 * 3600)
        await r.lpush(RECENT_KEY, json.dumps(entry))
        await r.ltrim(RECENT_KEY, 0, RECENT_LENGTH - 1)
    except Exception as exc:
        logger.error("fill deviation could not be counted (Redis): %s", exc)


async def count_24h(now: Optional[float] = None) -> Optional[int]:
    """Deviations today plus yesterday (UTC): a rolling day, cheap to keep."""
    try:
        r = _redis()
        t = now or time.time()
        vals = [await r.get(DAY_KEY.format(day=_day(t))), await r.get(DAY_KEY.format(day=_day(t - 86400)))]
        return sum(int(v or 0) for v in vals)
    except Exception:
        return None


async def recent(limit: int = 50) -> list:
    try:
        rows = await _redis().lrange(RECENT_KEY, 0, limit - 1)
    except Exception:
        return []
    out = []
    for raw in rows:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out
