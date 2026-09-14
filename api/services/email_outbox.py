"""Email outbox: purchase-path emails are queued, retried with backoff, never sent inline.

The purchase path only renders the message and pushes it onto a Redis sorted
set (score = when it is next due; shared by every worker, survives restarts)
and returns at once. The scheduler drains due messages after each run and
every minute, under a time budget, so a hanging provider delays nothing but
its own emails.

A failed attempt (transport error, timeout, 5xx) re-schedules the message
with backoff — 30 s, 1, 2, 4, 8 minutes, about 15 minutes in total — and
failure / security emails get more attempts than confirmations. When the
attempts are exhausted the message is logged at ERROR with recipient and
template, recorded in the dead-letter list (`GET /api/admin/emails/undelivered`)
and, when the user is known, in their notification history; an hourly
failure counter feeds the `email_delivery_failures` admin alert and a Slack
notice. Nothing here ever fails, retries or delays a purchase. The queue is
capped (oldest dropped, logged) and draining ignores the admin pause switch:
pausing stops trading, not notifications.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from api.config import settings
from api.integrations import resend

logger = logging.getLogger("autobuy.email_outbox")

OUTBOX_KEY = "autobuy:email:outbox"            # ZSET: member = JSON message, score = due time (unix)
DEAD_LETTER_KEY = "autobuy:email:undelivered"  # LIST: newest first, capped
FAILURES_HOUR_KEY = "autobuy:email:failures:{hour}"   # final give-ups per UTC hour
ALERT_SENT_KEY = "autobuy:email:failure-alert:{hour}"
DRAIN_BUDGET_SECONDS = 20.0      # per drain call
PER_EMAIL_TIMEOUT_SECONDS = 10.0
MAX_QUEUE_LENGTH = 10_000        # beyond this, the earliest-due messages are dropped (logged)
DEAD_LETTER_LENGTH = 500

# Delay before attempt n+1 after attempt n failed (seconds). Cumulative ≈ 15.5 min.
BACKOFF_SECONDS = (30, 60, 120, 240, 480)
# Attempts per category: confirmations give up after ~3.5 min, failure/security emails after ~15 min.
MAX_ATTEMPTS = {"confirmation": 4, "failure": 6, "security": 6}
CATEGORIES = tuple(MAX_ATTEMPTS)


def failure_alert_threshold() -> int:
    return int(getattr(settings, "email_failure_alert_threshold", 5) or 5)


def _redis():
    from api.services.redis_client import get_redis
    return get_redis()


def _hour_key(now: float) -> str:
    return FAILURES_HOUR_KEY.format(hour=time.strftime("%Y-%m-%dT%H", time.gmtime(now)))


def max_attempts(category: str) -> int:
    return MAX_ATTEMPTS.get(category, MAX_ATTEMPTS["confirmation"])


def backoff_for(attempt: int) -> int:
    """Seconds to wait after the `attempt`-th failure (1-based)."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


async def enqueue(to: str, subject: str, html: str, text: str, template: str = "generic", *,
                  category: str = "confirmation", user_id: Optional[str] = None,
                  now: Optional[float] = None) -> bool:
    """Queue one message, due immediately. Returns False (after an ERROR log) when Redis is unavailable."""
    now = time.time() if now is None else now
    category = category if category in CATEGORIES else "confirmation"
    msg = {"id": uuid.uuid4().hex, "to": to, "subject": subject, "html": html, "text": text, "template": template,
           "category": category, "user_id": user_id, "queued_at": now, "attempts": 0, "last_error": None}
    try:
        r = _redis()
        await r.zadd(OUTBOX_KEY, {json.dumps(msg): now})
        size = await r.zcard(OUTBOX_KEY)
        if size > MAX_QUEUE_LENGTH:
            dropped = await r.zremrangebyrank(OUTBOX_KEY, 0, size - MAX_QUEUE_LENGTH - 1)
            logger.error("email outbox over its cap of %d messages: %d earliest-due message(s) dropped (newest kept: to=%s template=%s)",
                         MAX_QUEUE_LENGTH, dropped, to, template)
        return True
    except Exception as exc:
        logger.error("email outbox unavailable, message not queued: to=%s template=%s subject=%r reason=%s",
                     to, template, subject, exc)
        return False


async def length() -> Optional[int]:
    try:
        return int(await _redis().zcard(OUTBOX_KEY))
    except Exception:
        return None


async def pending(limit: int = 100) -> list:
    """Queued messages (without bodies) with their attempts and next due time, earliest first."""
    try:
        rows = await _redis().zrange(OUTBOX_KEY, 0, limit - 1, withscores=True)
    except Exception:
        return []
    out = []
    for raw, score in rows:
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        out.append({k: m.get(k) for k in ("id", "to", "subject", "template", "category", "user_id", "queued_at",
                                           "attempts", "last_error")} | {"next_attempt_at": score})
    return out


async def undelivered(limit: int = 100) -> list:
    """Messages that exhausted their attempts (newest first, bodies omitted)."""
    try:
        rows = await _redis().lrange(DEAD_LETTER_KEY, 0, limit - 1)
    except Exception:
        return []
    out = []
    for raw in rows:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


async def failures_this_hour(now: Optional[float] = None) -> Optional[int]:
    try:
        v = await _redis().get(_hour_key(time.time() if now is None else now))
        return int(v or 0)
    except Exception:
        return None


async def _give_up(r, msg: dict, now: float) -> None:
    """Final outcome: dead-letter entry, notification-history row, hourly counter, alert."""
    record = {k: msg.get(k) for k in ("id", "to", "subject", "template", "category", "user_id", "queued_at",
                                       "attempts", "last_error")} | {"gave_up_at": now}
    logger.error("email NOT delivered after %d attempt(s), giving up: to=%s template=%s category=%s subject=%r last_error=%s",
                 msg["attempts"], msg["to"], msg.get("template"), msg.get("category"), msg["subject"], msg.get("last_error"))
    try:
        await r.lpush(DEAD_LETTER_KEY, json.dumps(record))
        await r.ltrim(DEAD_LETTER_KEY, 0, DEAD_LETTER_LENGTH - 1)
        key = _hour_key(now)
        count = await r.incr(key)
        await r.expire(key, 2 * 3600)
    except Exception as exc:
        logger.error("email dead-letter bookkeeping failed: %s", exc)
        count = None
    if msg.get("user_id"):
        await _record_notification(msg, "failed")
    if count is not None and count > failure_alert_threshold():
        await _alert_once(r, now, count)


async def _record_notification(msg: dict, status: str) -> None:
    """Notification-history row for the user (what they can see was or was not delivered)."""
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool
        from api.database import DATABASE_URL
        from api.models import Notification
        engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                db.add(Notification(user_id=uuid.UUID(str(msg["user_id"])), channel="email",
                                    template=msg.get("template") or "generic", subject=msg["subject"][:255], status=status))
                await db.commit()
        finally:
            await engine.dispose()
    except Exception as exc:
        logger.warning("could not record notification outcome for user %s: %s", msg.get("user_id"), exc)


async def _alert_once(r, now: float, count: int) -> None:
    key = ALERT_SENT_KEY.format(hour=time.strftime("%Y-%m-%dT%H", time.gmtime(now)))
    try:
        if not await r.set(key, "1", nx=True, ex=2 * 3600):
            return
    except Exception:
        return
    logger.critical("EMAIL DELIVERY: %d emails could not be delivered this hour (threshold %d)", count, failure_alert_threshold())
    try:
        from api.services.slack_service import send_slack
        await send_slack(f":email: :rotating_light: AutoBuy email delivery: {count} emails could not be delivered this hour "
                         f"(threshold {failure_alert_threshold()}). See /api/admin/emails/undelivered.")
    except Exception:
        logger.exception("email failure alert could not be sent")


async def drain(budget_seconds: float = DRAIN_BUDGET_SECONDS,
                per_email_timeout: float = PER_EMAIL_TIMEOUT_SECONDS,
                now: Optional[float] = None) -> dict:
    """Send every due message until the queue has no due messages or the budget
    is spent. Failures are re-scheduled with backoff; exhausted messages go to
    the dead letter. Never raises. Returns {sent, retried, gave_up, remaining}."""
    counts = {"sent": 0, "retried": 0, "gave_up": 0, "remaining": 0}
    started = time.perf_counter()
    clock = now
    try:
        r = _redis()
    except Exception as exc:
        logger.error("email outbox drain skipped: Redis unavailable (%s)", exc)
        return counts
    seen: set = set()
    while time.perf_counter() - started < budget_seconds:
        t = time.time() if clock is None else clock
        try:
            due = await r.zrangebyscore(OUTBOX_KEY, "-inf", t, start=0, num=1)
        except Exception as exc:
            logger.error("email outbox read failed: %s", exc)
            break
        if not due:
            break
        raw = due[0]
        if raw in seen:   # re-scheduled inside this drain with a due time <= now (fixed clock): stop looping
            break
        try:
            removed = await r.zrem(OUTBOX_KEY, raw)
        except Exception as exc:
            logger.error("email outbox read failed: %s", exc)
            break
        if not removed:
            continue   # another worker took it
        try:
            msg = json.loads(raw)
        except ValueError:
            logger.error("email outbox: dropped an unreadable message (%r)", str(raw)[:120])
            continue
        remaining_budget = budget_seconds - (time.perf_counter() - started)
        timeout = max(min(per_email_timeout, remaining_budget), 0.05)
        msg["attempts"] = int(msg.get("attempts") or 0) + 1
        error: Optional[str] = None
        try:
            await asyncio.wait_for(
                resend.send_email(msg["to"], msg["subject"], msg["html"], text_content=msg.get("text"),
                                  template=msg.get("template", "generic")),
                timeout)
        except asyncio.TimeoutError:
            error = f"timeout after {timeout:.1f}s"
        except resend.ResendNotConfigured as exc:
            error = f"not configured: {exc}"
        except Exception as exc:
            error = str(exc)[:300]
        if error is None:
            counts["sent"] += 1
            if msg["attempts"] > 1:
                logger.info("email delivered on attempt %d: to=%s template=%s", msg["attempts"], msg["to"], msg.get("template"))
                if msg.get("user_id"):
                    await _record_notification(msg, "sent")
            continue
        msg["last_error"] = error
        if msg["attempts"] >= max_attempts(msg.get("category", "confirmation")):
            counts["gave_up"] += 1
            await _give_up(r, msg, t)
            continue
        delay = backoff_for(msg["attempts"])
        logger.warning("email attempt %d/%d failed, retrying in %ds: to=%s template=%s reason=%s",
                       msg["attempts"], max_attempts(msg.get("category", "confirmation")), delay,
                       msg["to"], msg.get("template"), error)
        try:
            new_raw = json.dumps(msg)
            await r.zadd(OUTBOX_KEY, {new_raw: t + delay})
            seen.add(new_raw)
            counts["retried"] += 1
        except Exception as exc:
            logger.error("email lost (could not re-queue after a failed attempt): to=%s template=%s reason=%s",
                         msg["to"], msg.get("template"), exc)
    try:
        counts["remaining"] = int(await r.zcard(OUTBOX_KEY))
    except Exception:
        pass
    if any(counts[k] for k in ("sent", "retried", "gave_up", "remaining")):
        logger.info("email outbox drained in %.0f ms: %s", (time.perf_counter() - started) * 1000, counts)
    return counts
