"""v0.7.4.1: each gunicorn worker runs its own scheduler, so the low-balance pass
logged three times per tick on the server. One SET NX per tick dedupes them."""
import logging

import pytest

from api.services.redis_client import get_redis
from api.tasks import scheduler as sch

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def clean_tick_keys():
    r = get_redis()

    async def wipe():
        keys = [k async for k in r.scan_iter("autobuy:job:*:tick")]
        if keys:
            await r.delete(*keys)
    await wipe()
    yield
    await wipe()


async def test_first_worker_wins_the_tick_and_the_others_skip():
    assert await sch._claim_tick("t") is True
    assert await sch._claim_tick("t") is False
    assert await sch._claim_tick("t") is False
    ttl = await get_redis().ttl("autobuy:job:t:tick")
    assert 0 < ttl <= sch.TICK_GUARD_TTL_SECONDS
    assert await sch._claim_tick("other") is True   # jobs do not share a key


async def test_a_skipped_tick_never_opens_the_database(monkeypatch):
    for name in ("low_balance_check", "admin_alert_digest", "weekly_digest"):
        assert await sch._claim_tick(name) is True

    def boom(*a, **k):
        raise AssertionError("database engine built on a skipped tick")
    monkeypatch.setattr(sch, "create_async_engine", boom)
    assert await sch._check_low_balances_once() == {"skipped": "tick"}
    assert await sch._alert_digest_once() == {"critical": [], "emailed": [], "recipients": [], "skipped": "tick"}
    assert await sch._send_weekly_digest_once() is False


async def test_redis_trouble_lets_the_job_through_to_the_advisory_lock(monkeypatch, caplog):
    class Broken:
        async def set(self, *a, **k):
            raise ConnectionError("redis down")
    monkeypatch.setattr("api.services.redis_client.get_redis", lambda: Broken())
    with caplog.at_level(logging.WARNING):
        assert await sch._claim_tick("low_balance_check") is True
    assert any("tick guard for low_balance_check unavailable" in r.getMessage() for r in caplog.records)
