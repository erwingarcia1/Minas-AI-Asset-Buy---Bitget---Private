"""APScheduler job wrappers and the scheduler main.py starts.

Moved verbatim from api/tasks/purchase_scheduler.py on 2026-09-22 (audit
Batch B3); that path re-exports everything here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Optional, Protocol

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from api.config import settings
from api.database import DATABASE_URL
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow
from api.services import bitget_market
from api.services.history import BUY_TYPES
from api.services.purchase_executor import (WITHDRAWAL_POLL_INTERVAL_MINUTES, _register_run_result,  # noqa: F401
                                            execute_due_purchases, poll_withdrawals)

logger = logging.getLogger("autobuy.tasks.purchases")

MISFIRE_GRACE_SECONDS = 120


INTERVAL_MINUTES = 5


async def _run_once() -> dict:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            counts = await execute_due_purchases(session)
    finally:
        await engine.dispose()
    # Emails queued by the run go out now, after every purchase is committed and
    # the heartbeat recorded, under a time budget: a slow provider cannot stall
    # the run and a failed email never touches a purchase.
    from api.services import email_outbox
    await email_outbox.drain()
    from api.services import telegram
    await telegram.flush()   # one summary line per collapsed burst; never raises
    return counts


def drain_email_outbox() -> dict:
    """APScheduler entry point (every minute): send emails queued by the API
    workers or left over from a previous run's budget."""
    from api.services import email_outbox
    try:
        return asyncio.run(email_outbox.drain())
    except Exception:
        logger.exception("email outbox drain failed")
        return {"sent": 0, "retried": 0, "gave_up": 0, "remaining": 0}


def execute_purchases() -> dict:
    """APScheduler entry point (sync, runs in the scheduler's worker thread)."""
    logger.info("execute_purchases called by APScheduler (pid=%s)", os.getpid())
    try:
        return asyncio.run(_run_once())
    except Exception as exc:
        logger.exception("auto-buy scheduler run failed")
        from api.services import scheduler_state
        scheduler_state.record_run_sync("autobuy", "error", detail=repr(exc))
        scheduler_state.bump_failures_sync("autobuy", ok=False)
        return {"executed": 0, "failed": 0, "skipped": 0}


async def _poll_once() -> dict:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await poll_withdrawals(session)
    finally:
        await engine.dispose()


def poll_withdrawals_job() -> dict:
    """APScheduler entry point: confirm sent withdrawals with the exchange."""
    try:
        return asyncio.run(_poll_once())
    except Exception as exc:
        logger.exception("withdrawal poll failed")
        from api.services import scheduler_state
        scheduler_state.record_run_sync("withdrawals", "error", detail=repr(exc))
        return {"confirmed": 0, "failed": 0, "pending": 0, "skipped": 0}


# Every gunicorn worker runs its own APScheduler, so a job fires once per worker
# within the same second. The advisory locks below serialise those runs but do
# not dedupe them (a pass that takes milliseconds is over before the next worker
# tries). The first SET NX on this key wins the tick; the others skip. Redis
# trouble lets the job through to the advisory lock rather than silencing it.
TICK_GUARD_TTL_SECONDS = 60


async def _claim_tick(name: str, ttl: int = TICK_GUARD_TTL_SECONDS) -> bool:
    try:
        from api.services.redis_client import get_redis
        return bool(await get_redis().set(f"autobuy:job:{name}:tick", str(os.getpid()), nx=True, ex=ttl))
    except Exception as exc:
        logger.warning("tick guard for %s unavailable (%s); relying on the advisory lock", name, exc)
        return True


ALERT_DIGEST_LOCK_KEY = 0x41424144  # "ABAD"


async def _alert_digest_once() -> dict:
    from api.services.admin_alerts import notify_critical_alerts
    if not await _claim_tick("admin_alert_digest"):
        return {"critical": [], "emailed": [], "recipients": [], "skipped": "tick"}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as lock_conn:
            got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ALERT_DIGEST_LOCK_KEY})).scalar()
            if not got:
                return {"critical": [], "emailed": [], "recipients": [], "skipped": "lock"}
            try:
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    return await notify_critical_alerts(session)
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ALERT_DIGEST_LOCK_KEY})
                await lock_conn.commit()
    finally:
        await engine.dispose()


def admin_alert_digest() -> dict:
    """APScheduler entry point (every 15 min): email admins about critical alerts,
    at most once per condition every 6 hours. Works without Slack."""
    try:
        return asyncio.run(_alert_digest_once())
    except Exception:
        logger.exception("admin alert digest failed")
        return {}


# Session-level advisory lock for the weekly digest: one worker sends, the
# other three find the lock taken and return.
DIGEST_LOCK_KEY = 0x41425744  # "ABWD"


async def _send_weekly_digest_once() -> bool:
    from api.services.email_campaigns import send_weekly_summaries
    if not await _claim_tick("weekly_digest"):
        logger.info("weekly digest skipped: another worker took this tick (pid=%s)", os.getpid())
        return False
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as lock_conn:
            got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": DIGEST_LOCK_KEY})).scalar()
            if not got:
                logger.info("weekly digest skipped: another worker holds the lock (pid=%s)", os.getpid())
                return False
            try:
                await send_weekly_summaries()
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": DIGEST_LOCK_KEY})
                await lock_conn.commit()
        return True
    finally:
        await engine.dispose()


# Session-level advisory lock for the low-balance check: one worker per pass.
LOW_BALANCE_LOCK_KEY = 0x41424C42  # "ABLB"


async def _check_low_balances_once() -> dict:
    from api.services.low_balance import check_low_balances
    from api.services import telegram
    if not await _claim_tick("low_balance_check"):
        return {"skipped": "tick"}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as lock_conn:
            got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOW_BALANCE_LOCK_KEY})).scalar()
            if not got:
                return {"skipped": "lock"}
            try:
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    result = await check_low_balances(session)
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOW_BALANCE_LOCK_KEY})
                await lock_conn.commit()
    finally:
        await engine.dispose()
    await telegram.flush()
    return result


def check_low_balances_job() -> dict:
    """APScheduler entry point (every 30 min): read-only pre-buy balance check;
    warns customers about a day and about two hours before an underfunded run.
    Never places an order."""
    try:
        return asyncio.run(_check_low_balances_once())
    except Exception:
        logger.exception("low-balance check failed")
        return {}


def send_weekly_digest() -> bool:
    """APScheduler entry point (Mondays 09:00 UTC): the weekly purchase summary emails."""
    logger.info("weekly digest job called by APScheduler (pid=%s)", os.getpid())
    try:
        return asyncio.run(_send_weekly_digest_once())
    except Exception:
        logger.exception("weekly digest job failed")
        return False


def build_scheduler():
    """The APScheduler instance main.py starts: the 5-minute execute_purchases
    interval job with a generous misfire grace (late wake-ups run instead of
    being discarded), the Monday 09:00 UTC weekly digest, and the
    miss/skip/error listener attached. Not started."""
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(execute_purchases, "interval", minutes=INTERVAL_MINUTES, id="execute_purchases",
                  max_instances=1, coalesce=True, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    sched.add_job(poll_withdrawals_job, "interval", minutes=WITHDRAWAL_POLL_INTERVAL_MINUTES, id="poll_withdrawals",
                  max_instances=1, coalesce=True, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    sched.add_job(drain_email_outbox, "interval", minutes=1, id="drain_email_outbox",
                  max_instances=1, coalesce=True, misfire_grace_time=55)
    sched.add_job(admin_alert_digest, "interval", minutes=15, id="admin_alert_digest",
                  max_instances=1, coalesce=True, misfire_grace_time=600)
    from api.services.low_balance import JOB_INTERVAL_MINUTES
    sched.add_job(check_low_balances_job, "interval", minutes=JOB_INTERVAL_MINUTES, id="low_balance_check",
                  max_instances=1, coalesce=True, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    sched.add_job(send_weekly_digest, "cron", day_of_week="mon", hour=9, minute=0, id="weekly_digest",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.add_listener(job_event_listener, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR)
    return sched


def job_event_listener(event) -> None:
    """APScheduler listener: surface the cases where execute_purchases is NOT
    called, on this module's logger, so they sit next to the run logs.

    Register with EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR.
    """
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
    if getattr(event, "job_id", None) not in ("execute_purchases", "poll_withdrawals"):
        return
    when = getattr(event, "scheduled_run_time", None)
    if event.code == EVENT_JOB_MISSED:
        logger.warning("execute_purchases window DISCARDED, not called: scheduled %s but the scheduler "
                       "woke later than misfire_grace_time (pid=%s)", when, os.getpid())
    elif event.code == EVENT_JOB_MAX_INSTANCES:
        logger.warning("execute_purchases window SKIPPED, not called: previous run still in progress "
                       "(scheduled %s, pid=%s)", when, os.getpid())
    elif event.code == EVENT_JOB_ERROR:
        logger.error("execute_purchases raised out of the job: %r (scheduled %s, pid=%s)",
                     getattr(event, "exception", None), when, os.getpid())
