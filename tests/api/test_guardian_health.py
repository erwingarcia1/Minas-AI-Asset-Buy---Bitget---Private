"""/health reports the Guardian worker from its Redis heartbeat (v0.9.1)."""
import json

from api.config import settings
from api.services import guardian_state


async def test_health_reports_a_missing_worker_as_not_running(client):
    from api.services.redis_client import get_redis
    await get_redis().delete(guardian_state.HEARTBEAT_KEY, guardian_state.PAUSE_KEY)
    async with client as c:
        r = await c.get("/health")
    g = r.json()["guardian"]
    assert g["running"] is False and g["enabled"] is False and g["paused"] is False
    assert g["last_pass_at"] is None and g["heartbeat_age_seconds"] is None


async def test_health_reports_a_fresh_heartbeat_and_the_pause_switch(client):
    from api.services.redis_client import get_redis
    r_ = get_redis()
    await r_.setex(guardian_state.HEARTBEAT_KEY, 180, json.dumps({
        "at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "enabled": True, "active_accounts": 3, "rules_valid": True, "commit": "abc"}))
    await guardian_state.pause("admin@example.com")
    try:
        async with client as c:
            g = (await c.get("/health")).json()["guardian"]
        assert g["running"] is True and g["active_accounts"] == 3 and g["paused"] is True
        assert g["heartbeat_age_seconds"] is not None and g["heartbeat_age_seconds"] < 5
        assert (await guardian_state.pause_info())["by"] == "admin@example.com"
    finally:
        await guardian_state.resume()
        await r_.delete(guardian_state.HEARTBEAT_KEY)


def test_a_stale_or_stopping_heartbeat_is_not_running():
    from datetime import datetime, timedelta, timezone
    stale = {"at": (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()}
    fresh = {"at": datetime.now(timezone.utc).isoformat()}
    assert guardian_state.running(stale) is False
    assert guardian_state.running(fresh) is True
    assert guardian_state.running({**fresh, "stopping": True}) is False
    assert guardian_state.running(None) is False
