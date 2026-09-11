"""Cross-worker scheduler state kept in Redis.

The Auto-Buy ``execute_purchases`` job runs inside every gunicorn worker
(every 5 minutes). Anything that must hold for *all* workers and survive an
API restart lives here, never in process memory:

* the admin **pause switch** (:func:`pause` / :func:`resume`): the job reads
  it at the start of every run and skips while it is set;
* a **heartbeat** per scheduler (last run time, result, detail, counts), so
  ``/health`` and the admin alerts describe the worker that actually ran,
  not the worker that happened to answer the request;
* a **consecutive-failure counter** per scheduler.

The pause switch fails CLOSED: when Redis cannot be read, callers skip the
run and log at ERROR instead of executing while an operator believes
everything is paused. Heartbeats are best effort (a Redis blip never breaks
a run).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis as redis_sync

from api.config import settings

logger = logging.getLogger("autobuy.scheduler_state")

SCHEDULERS = ("autobuy",)
PAUSE_KEY = "autobuy:schedulers:paused"
_HEARTBEAT_KEY = "autobuy:schedulers:{name}:last_run"
_FAILURES_KEY = "autobuy:schedulers:{name}:consecutive_failures"

__all__ = [
    "PauseStateUnavailable", "SCHEDULERS", "PAUSE_KEY",
    "pause", "resume", "pause_info", "is_paused", "pause_info_sync",
    "record_run", "record_run_sync", "bump_failures", "bump_failures_sync", "set_failures",
    "last_run", "consecutive_failures", "status",
]


class PauseStateUnavailable(RuntimeError):
    """Redis could not be read; the caller must fail closed (skip the run)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_client() -> redis_sync.Redis:
    # A fresh, short-lived connection: the Auto-Buy job runs in APScheduler's
    # worker thread (no event loop) once every few minutes.
    return redis_sync.Redis.from_url(settings.redis_url, decode_responses=True,
                                     socket_timeout=3, socket_connect_timeout=3)


def _parse_pause(raw: Optional[str]) -> dict:
    if not raw:
        return {"paused": False, "since": None, "by": None}
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return {"paused": True, "since": data.get("since"), "by": data.get("by")}


def _parse_run(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ------------------------------------------------------------------ pause switch

async def pause(by: str) -> dict:
    """Set the switch. Every scheduler in every worker skips from its next run on."""
    from api.services.redis_client import get_redis
    info = {"since": _now_iso(), "by": by}
    await get_redis().set(PAUSE_KEY, json.dumps(info))
    logger.warning("schedulers PAUSED by admin %s", by)
    return {"paused": True, **info}


async def resume() -> None:
    from api.services.redis_client import get_redis
    await get_redis().delete(PAUSE_KEY)
    logger.warning("schedulers RESUMED")


async def pause_info() -> dict:
    """``{"paused": bool, "since": iso|None, "by": admin_id|None}``; raises
    :class:`PauseStateUnavailable` when Redis cannot be read."""
    from api.services.redis_client import get_redis
    try:
        raw = await get_redis().get(PAUSE_KEY)
    except Exception as exc:  # connection refused, timeout, auth...
        raise PauseStateUnavailable(str(exc)) from exc
    return _parse_pause(raw)


async def is_paused() -> bool:
    return (await pause_info())["paused"]


def pause_info_sync() -> dict:
    """Thread-safe variant for code running outside an event loop."""
    try:
        client = _sync_client()
        try:
            raw = client.get(PAUSE_KEY)
        finally:
            client.close()
    except Exception as exc:
        raise PauseStateUnavailable(str(exc)) from exc
    return _parse_pause(raw)


# ------------------------------------------------------------------ heartbeats

def _run_payload(result: str, detail: Optional[str], counts: Optional[dict]) -> str:
    return json.dumps({"at": _now_iso(), "result": result,
                       "detail": (detail or None) and str(detail)[:300], "counts": counts})


async def record_run(name: str, result: str, detail: Optional[str] = None,
                     counts: Optional[dict] = None) -> None:
    """Best-effort heartbeat: result is ``ok`` | ``failed`` | ``error`` | ``skipped_paused``."""
    from api.services.redis_client import get_redis
    try:
        await get_redis().set(_HEARTBEAT_KEY.format(name=name), _run_payload(result, detail, counts))
    except Exception:
        logger.warning("scheduler heartbeat not recorded (Redis unavailable): %s -> %s", name, result)


def record_run_sync(name: str, result: str, detail: Optional[str] = None,
                    counts: Optional[dict] = None) -> None:
    try:
        client = _sync_client()
        try:
            client.set(_HEARTBEAT_KEY.format(name=name), _run_payload(result, detail, counts))
        finally:
            client.close()
    except Exception:
        logger.warning("scheduler heartbeat not recorded (Redis unavailable): %s -> %s", name, result)


async def bump_failures(name: str, ok: bool) -> int:
    """Reset the counter on a good run, increment it on a bad one. Returns the new value."""
    from api.services.redis_client import get_redis
    key = _FAILURES_KEY.format(name=name)
    try:
        if ok:
            await get_redis().set(key, 0)
            return 0
        return int(await get_redis().incr(key))
    except Exception:
        logger.warning("scheduler failure counter not updated (Redis unavailable): %s", name)
        return -1


def bump_failures_sync(name: str, ok: bool) -> int:
    key = _FAILURES_KEY.format(name=name)
    try:
        client = _sync_client()
        try:
            if ok:
                client.set(key, 0)
                return 0
            return int(client.incr(key))
        finally:
            client.close()
    except Exception:
        logger.warning("scheduler failure counter not updated (Redis unavailable): %s", name)
        return -1


async def set_failures(name: str, value: int) -> None:
    from api.services.redis_client import get_redis
    try:
        await get_redis().set(_FAILURES_KEY.format(name=name), int(value))
    except Exception:
        logger.warning("scheduler failure counter not updated (Redis unavailable): %s", name)


async def last_run(name: str) -> Optional[dict]:
    from api.services.redis_client import get_redis
    return _parse_run(await get_redis().get(_HEARTBEAT_KEY.format(name=name)))


async def consecutive_failures(name: str) -> int:
    from api.services.redis_client import get_redis
    raw = await get_redis().get(_FAILURES_KEY.format(name=name))
    return int(raw) if raw else 0


async def status(name: str) -> dict:
    """Heartbeat + failure counter for ``/health`` and the admin alerts.
    Never raises: a Redis outage is reported as ``redis: "unavailable"``."""
    try:
        run = await last_run(name)
        fails = await consecutive_failures(name)
    except Exception:
        return {"last_run_at": None, "last_result": None, "last_detail": None,
                "last_counts": None, "consecutive_failures": None, "redis": "unavailable"}
    run = run or {}
    return {"last_run_at": run.get("at"), "last_result": run.get("result"),
            "last_detail": run.get("detail"), "last_counts": run.get("counts"),
            "consecutive_failures": fails, "redis": "ok"}
