"""Shared async Redis client (token revocation, login lockout, rate limits)."""
import asyncio
import logging
from typing import Optional

import redis.asyncio as redis

from api.config import settings

logger = logging.getLogger("autobuy.redis")

_client: Optional[redis.Redis] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None


def get_redis() -> redis.Redis:
    """Async Redis client bound to the running event loop.

    Rebuilt if the loop changed (pytest-asyncio runs each test on a fresh
    loop; a cached client would hold connections on a closed loop)."""
    global _client, _client_loop
    loop = asyncio.get_event_loop()
    if _client is None or _client_loop is not loop:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
        _client_loop = loop
    return _client


async def close_redis() -> None:
    global _client, _client_loop
    if _client is not None:
        await _client.aclose()
        _client = None
        _client_loop = None
