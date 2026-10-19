"""The daily MEDIUM/LOW digest, inside the worker instead of a host cron.

Guardian's box ran `0 8 * * * sentinel digest` from root's crontab, which
is exactly the kind of thing that does not survive moving house. The worker
already wakes every scheduler pass; once a day, after the configured UTC
hour, it sends the digest and records the date in Redis so a restart during
the day does not send it twice. Nothing to report sends nothing, but still
records the date (the digest module itself refuses empty digests).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("guardian.digest")

LAST_SENT_KEY = "autobuy:guardian:digest:last_date"


class DailyDigest:
    def __init__(self, db, channels: Callable[[], list], *, hour_utc: int, redis_url: str,
                 hours: int = 24):
        self._db = db
        self._channels = channels
        self._hour = max(0, min(23, int(hour_utc)))
        self._url = redis_url
        self._hours = hours
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis
            self._client = redis.Redis.from_url(self._url, decode_responses=True,
                                                socket_timeout=3.0, socket_connect_timeout=3.0)
        return self._client

    def run_if_due(self, now: Optional[datetime] = None) -> bool:
        """True when a digest went out this call."""
        now = now or datetime.now(timezone.utc)
        if now.hour < self._hour:
            return False
        today = now.date().isoformat()
        try:
            if self._redis().get(LAST_SENT_KEY) == today:
                return False
        except Exception as exc:  # noqa: BLE001 - without the marker we might send twice; skip instead
            logger.warning("digest marker unreadable (Redis): %s - digest skipped this pass", type(exc).__name__)
            self._client = None
            return False
        from guardian.alerts.digest import send_digest
        targets = self._channels()
        sent = False
        if not targets:
            logger.warning("daily digest: no channel configured (Telegram or admin email); nothing sent")
        else:
            try:
                sent = send_digest(self._db, targets, hours=self._hours)
            except Exception as exc:  # noqa: BLE001 - a digest failure must not stop the scheduler
                logger.error("daily digest failed: %s", exc)
        try:
            self._redis().set(LAST_SENT_KEY, today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("digest marker not written (Redis): %s", type(exc).__name__)
        logger.info("daily digest %s", "sent" if sent else "had nothing to report")
        return sent
