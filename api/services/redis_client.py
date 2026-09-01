"""Shared async Redis client (token revocation, login lockout, rate limits)."""
import logging
from typing import Optional

import redis.asyncio as redis

from api.config import settings

logger = logging.getLogger("autobuy.redis")

_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
