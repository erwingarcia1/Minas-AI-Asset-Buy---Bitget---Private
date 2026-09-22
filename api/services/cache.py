"""One read-through JSON cache over Redis (audit Batch B2, 2026-09-22).

Three copies used to exist with three failure policies. The policy is now a
parameter and every caller passes the one it always had:

* bitget_market (sync + async): cache read failure -> warn and produce;
  producer failure -> raise MarketDataUnavailable (fail closed, the purchase
  is refused); cache write failure -> warn.
* market_data (async): cache read failure -> warn and produce; producer
  failure -> warn and return None (analytics degrade); cache write failure ->
  silent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("autobuy.cache")


async def cached_json(key: str, ttl: int, produce: Callable[[], Awaitable[Any]], *,
                      on_produce_error: Callable[[Exception], Any], read_warning: str,
                      write_warning: Optional[str] = None):
    """Async read-through. `on_produce_error` either raises or returns the fallback value.
    `write_warning` None = a failed cache write is silent."""
    from api.services.redis_client import get_redis
    try:
        raw = await get_redis().get(key)
        if raw is not None:
            return json.loads(raw)
    except Exception:
        logger.warning(read_warning, key)
    try:
        value = await produce()
    except Exception as exc:
        return on_produce_error(exc)
    try:
        await get_redis().setex(key, ttl, json.dumps(value))
    except Exception:
        if write_warning:
            logger.warning(write_warning, key)
    return value


def cached_json_sync(key: str, ttl: int, produce: Callable[[], Any], *, redis_factory,
                     on_produce_error: Callable[[Exception], Any], read_warning: str,
                     write_warning: Optional[str] = None):
    """Sync twin for callers that run in a worker thread (no event loop)."""
    try:
        r = redis_factory()
        try:
            raw = r.get(key)
            if raw is not None:
                return json.loads(raw)
        finally:
            r.close()
    except Exception:
        logger.warning(read_warning, key)
    try:
        value = produce()
    except Exception as exc:
        return on_produce_error(exc)
    try:
        r = redis_factory()
        try:
            r.setex(key, ttl, json.dumps(value))
        finally:
            r.close()
    except Exception:
        if write_warning:
            logger.warning(write_warning, key)
    return value
