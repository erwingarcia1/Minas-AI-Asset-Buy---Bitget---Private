"""Email outbox: purchase-path emails are queued, never sent inline.

The executor used to call Resend up to three times per purchase with a
10-second timeout each, so a slow provider could stall a scheduler run.
Now the purchase path only pushes a fully rendered message onto a Redis
list (shared by every worker, survives restarts) and returns at once. The
scheduler drains the list after each run and every minute, under a time
budget, so a hanging provider delays nothing but its own emails.

A failed or timed-out email is logged and dropped; it never fails, retries
or delays a purchase.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from api.integrations import resend

logger = logging.getLogger("autobuy.email_outbox")

OUTBOX_KEY = "autobuy:email:outbox"
DRAIN_BUDGET_SECONDS = 20.0      # per drain call
PER_EMAIL_TIMEOUT_SECONDS = 10.0
MAX_QUEUE_LENGTH = 10_000        # beyond this, oldest messages are dropped (logged)


def _redis():
    from api.services.redis_client import get_redis
    return get_redis()


async def enqueue(to: str, subject: str, html: str, text: str, template: str = "generic") -> bool:
    """Queue one message. Returns False (after an ERROR log) when Redis is unavailable."""
    payload = json.dumps({"to": to, "subject": subject, "html": html, "text": text, "template": template,
                          "queued_at": time.time()})
    try:
        r = _redis()
        length = await r.rpush(OUTBOX_KEY, payload)
        if length > MAX_QUEUE_LENGTH:
            await r.ltrim(OUTBOX_KEY, -MAX_QUEUE_LENGTH, -1)
            logger.error("email outbox over %d messages; oldest dropped", MAX_QUEUE_LENGTH)
        return True
    except Exception as exc:
        logger.error("email outbox unavailable (%s): to=%s subject=%r", exc, to, subject)
        return False


async def length() -> Optional[int]:
    try:
        return int(await _redis().llen(OUTBOX_KEY))
    except Exception:
        return None


async def drain(budget_seconds: float = DRAIN_BUDGET_SECONDS,
                per_email_timeout: float = PER_EMAIL_TIMEOUT_SECONDS) -> dict:
    """Send queued messages until the queue is empty or the budget is spent.
    Never raises. Returns {sent, failed, timed_out, remaining}."""
    counts = {"sent": 0, "failed": 0, "timed_out": 0, "remaining": 0}
    started = time.perf_counter()
    try:
        r = _redis()
    except Exception as exc:
        logger.error("email outbox drain skipped: Redis unavailable (%s)", exc)
        return counts
    while time.perf_counter() - started < budget_seconds:
        try:
            raw = await r.lpop(OUTBOX_KEY)
        except Exception as exc:
            logger.error("email outbox read failed: %s", exc)
            break
        if raw is None:
            break
        try:
            msg = json.loads(raw)
        except ValueError:
            counts["failed"] += 1
            logger.error("email outbox: dropped unreadable message")
            continue
        remaining_budget = budget_seconds - (time.perf_counter() - started)
        timeout = max(min(per_email_timeout, remaining_budget), 0.05)
        try:
            await asyncio.wait_for(
                resend.send_email(msg["to"], msg["subject"], msg["html"], text_content=msg.get("text"),
                                  template=msg.get("template", "generic")),
                timeout)
            counts["sent"] += 1
        except asyncio.TimeoutError:
            counts["timed_out"] += 1
            logger.error("email dropped after %.1fs timeout: to=%s subject=%r", timeout, msg["to"], msg["subject"])
        except resend.ResendNotConfigured:
            counts["failed"] += 1   # already logged as skipped by the integration
        except Exception as exc:
            counts["failed"] += 1
            logger.error("email dropped: to=%s subject=%r reason=%s", msg["to"], msg["subject"], exc)
    try:
        counts["remaining"] = int(await r.llen(OUTBOX_KEY))
    except Exception:
        pass
    if any(counts[k] for k in ("sent", "failed", "timed_out", "remaining")):
        logger.info("email outbox drained in %.0f ms: %s", (time.perf_counter() - started) * 1000, counts)
    return counts
