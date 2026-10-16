"""The Guardian worker's heartbeat and pause switch, read and written from the API.

The worker (guardian/heartbeat.py) writes one JSON heartbeat per scheduler
pass with a 180 s TTL and reads the pause switch; this is the API's side of
the same two Redis keys, for `/health` and the admin pause/resume routes.
Separate from the trading pause (`autobuy:schedulers:paused`): pausing
watching and pausing buying are different decisions.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from api.config import settings

logger = logging.getLogger("autobuy.guardian_state")

HEARTBEAT_KEY = "autobuy:guardian:heartbeat"
PAUSE_KEY = "autobuy:guardian:paused"
STALE_AFTER_S = 180


def _redis():
    from api.services.redis_client import get_redis
    return get_redis()


async def heartbeat() -> Optional[dict]:
    """The worker's last heartbeat, or None when absent, expired or unreadable."""
    try:
        raw = await _redis().get(HEARTBEAT_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("guardian heartbeat unreadable (Redis): %s", type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def heartbeat_age_seconds(beat: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    if not beat or not beat.get("at"):
        return None
    try:
        at = datetime.fromisoformat(beat["at"]).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, (now if now is not None else time.time()) - at)


def running(beat: Optional[dict], now: Optional[float] = None) -> bool:
    age = heartbeat_age_seconds(beat, now)
    return age is not None and age < STALE_AFTER_S and not beat.get("stopping")


async def pause_info() -> dict:
    """{paused, since, by}; paused is None when Redis is unreadable (the worker then fails closed)."""
    try:
        raw = await _redis().get(PAUSE_KEY)
    except Exception:  # noqa: BLE001
        return {"paused": None, "since": None, "by": None}
    if not raw:
        return {"paused": False, "since": None, "by": None}
    try:
        info = json.loads(raw)
    except (TypeError, ValueError):
        info = {}
    return {"paused": True, "since": info.get("since"), "by": info.get("by")}


async def pause(by: str) -> None:
    await _redis().set(PAUSE_KEY, json.dumps({"since": datetime.now(timezone.utc).isoformat(), "by": by}))


async def resume() -> None:
    await _redis().delete(PAUSE_KEY)


async def health() -> dict:
    """The `guardian` block of /health."""
    beat = await heartbeat()
    pause_state = await pause_info()
    return {
        "enabled": settings.guardian_enabled,
        "running": running(beat),
        "paused": pause_state["paused"],
        "paused_since": pause_state["since"],
        "active_accounts": (beat or {}).get("active_accounts"),
        "last_pass_at": (beat or {}).get("at"),
        "heartbeat_age_seconds": (round(a, 1) if (a := heartbeat_age_seconds(beat)) is not None else None),
        "rules_valid": (beat or {}).get("rules_valid"),
        "commit": (beat or {}).get("commit"),
    }
