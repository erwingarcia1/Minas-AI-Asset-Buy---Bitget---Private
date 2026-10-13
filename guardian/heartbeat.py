"""The worker's pulse and the operator's pause switch, in Redis.

The API cannot see inside the worker container, and compose's healthcheck
only knows whether the process is alive. So every scheduler pass writes one
JSON heartbeat with a short TTL: `/health` (api/services/guardian_state.py)
reads it to say `guardian.running`, and the deploy refuses to call the box
healthy until it is fresh. The same pass reads the pause switch admins flip
from the admin pages. Both are written whether GUARDIAN_ENABLED is on or off:
an idle worker still proves it is alive.

Fail closed on the pause switch: a Redis we cannot read is a reason to skip
the pass, not to poll (the platform's trading pause works the same way).
The heartbeat itself fails open: a Redis blip must not stop detection, it
only makes `/health` say the worker is stale until the next pass.

A file is touched alongside, for compose's own healthcheck inside the
container (no Redis client needed there).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("guardian.heartbeat")

HEARTBEAT_KEY = "autobuy:guardian:heartbeat"
PAUSE_KEY = "autobuy:guardian:paused"
HEARTBEAT_TTL_S = 180
HEARTBEAT_FILE = "/tmp/guardian.heartbeat"


class Heartbeat:
    def __init__(self, redis_url: str, *, enabled: bool, commit: str = "unknown",
                 file_path: str = HEARTBEAT_FILE, ttl_seconds: int = HEARTBEAT_TTL_S):
        self._url = redis_url
        self._enabled = enabled
        self._commit = commit
        self._file = file_path
        self._ttl = ttl_seconds
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis
            self._client = redis.Redis.from_url(self._url, decode_responses=True,
                                                socket_timeout=3.0, socket_connect_timeout=3.0)
        return self._client

    def beat(self, snapshot: Optional[dict] = None) -> bool:
        """Publish one heartbeat. True when Redis took it."""
        payload = {"at": datetime.now(timezone.utc).isoformat(), "enabled": self._enabled,
                   "commit": self._commit, **(snapshot or {})}
        self._touch_file()
        try:
            self._redis().setex(HEARTBEAT_KEY, self._ttl, json.dumps(payload, default=str))
            return True
        except Exception as exc:  # noqa: BLE001 - a heartbeat must never stop the loop it reports on
            logger.warning("heartbeat not written (Redis): %s", type(exc).__name__)
            self._client = None
            return False

    def paused(self) -> bool:
        """The admin pause switch. Unreadable = paused: fail closed."""
        try:
            return bool(self._redis().get(PAUSE_KEY))
        except Exception as exc:  # noqa: BLE001
            logger.error("pause switch unreadable (Redis): %s - treating as paused", type(exc).__name__)
            self._client = None
            return True

    def _touch_file(self) -> None:
        try:
            with open(self._file, "a"):
                os.utime(self._file, None)
        except OSError as exc:
            logger.debug("heartbeat file not touched: %s", exc)

    def stopping(self) -> None:
        self.beat({"stopping": True})


def read_heartbeat(redis_url: str) -> Optional[dict]:
    """The last heartbeat, or None when absent, expired or unreadable
    (synchronous; the API has its async twin in api/services/guardian_state.py)."""
    try:
        import redis
        raw = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=3.0).get(HEARTBEAT_KEY)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def heartbeat_age_seconds(beat: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    if not beat or not beat.get("at"):
        return None
    try:
        at = datetime.fromisoformat(beat["at"]).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, (now if now is not None else time.time()) - at)
