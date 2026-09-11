"""Auto-Buy execution job: every 5 minutes, execute due purchases.

For each ``Purchase`` with status 'active' and next_execution <= now whose
owner is KYC-verified and not suspended:

1. load and decrypt the exchange account credentials,
2. market-buy `amount_usd` worth of the coin, then branch on
   ``purchase_type``: ``autobuy`` withdraws the filled amount to the wallet on
   the purchase's network (transaction tx_type = withdrawal); ``dca`` skips the
   withdrawal and the coins accumulate on the exchange (tx_type =
   accumulation). Both steps are simulated when AUTOBUY_DRY_RUN=1 (default),
3. record a ``PurchaseTransaction`` (pending -> success / failed),
4. on success advance ``next_execution`` by the frequency (one-time purchases
   complete); on failure count the attempt and mark the purchase 'failed'
   after MAX_ATTEMPTS consecutive failures — until then it stays due and is
   retried on the next run.

``execute_purchases()`` is the synchronous APScheduler entry point; it runs
the async core on its own event loop with a NullPool engine so it is safe in
a background thread. ``execute_due_purchases(session, now, gateway)`` is the
testable core.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Optional, Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from api.config import settings
from api.database import DATABASE_URL
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow

logger = logging.getLogger("autobuy.tasks.purchases")

MAX_ATTEMPTS = 3
BATCH_LIMIT = 200
# Session-level Postgres advisory lock held for the whole run: with one
# scheduler per gunicorn worker, only the first worker to reach a window
# executes; the others log and return. Distinct from the recurring tick's key.
ADVISORY_LOCK_KEY = 0x41425032  # "ABP2"
MISFIRE_GRACE_SECONDS = 120
INTERVAL_MINUTES = 5

# Exchange network codes for withdrawals (Binance naming; testnet uses the same
# code against the exchange's testnet endpoint).
WITHDRAW_NETWORK_CODES = {"BTC": "BTC", "ETH": "ETH", "USDT": "ETH", "USDC": "ETH", "SOL": "SOL"}


class ExecutionError(Exception):
    """A failed attempt; the message is stored on the transaction."""


class ExchangeGateway(Protocol):
    """What the scheduler needs from an exchange; production uses ExchangeClientGateway,
    tests substitute a fake."""

    def execute(self, account: APIKey, purchase: Purchase, dry_run: bool) -> dict:
        """Buy and withdraw. Returns {exchange_tx_id, amount_executed, fee}. Raises ExecutionError."""
        ...


class ExchangeClientGateway:
    """Buys via the platform's venue-agnostic ExchangeClient, then withdraws."""

    def execute(self, account: APIKey, purchase: Purchase, dry_run: bool) -> dict:
        from api.routes.exchange import decrypt_value
        from api.services.binance_client import BinanceClientError, ExchangeClient
        try:
            client = ExchangeClient(decrypt_value(account.encrypted_api_key),
                                    decrypt_value(account.encrypted_api_secret),
                                    decrypt_value(account.encrypted_passphrase) if account.encrypted_passphrase else "",
                                    exchange=account.exchange)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            order = client.execute_order(symbol, purchase.amount_usd, dry_run=dry_run,
                                         client_oid=f"ab-{purchase.id.hex[:12]}")
            amount = float(order["amount_crypto"])
            if purchase.purchase_type == "dca":
                # accumulate: coins stay on the exchange; the order id is the reference
                return {"exchange_tx_id": str(order["order_id"]), "amount_executed": amount, "fee": 0.0,
                        "order_id": order["order_id"], "tx_type": "accumulation"}
            if not purchase.wallet_address:
                raise ExecutionError("no wallet address on an autobuy purchase")
            withdrawal = client.withdraw(purchase.crypto_symbol, purchase.wallet_address, amount,
                                         network=WITHDRAW_NETWORK_CODES.get(purchase.crypto_symbol),
                                         client_oid=f"abw-{purchase.id.hex[:12]}", dry_run=dry_run)
        except BinanceClientError as exc:
            raise ExecutionError(f"{exc.code}: {exc}") from exc
        except Exception as exc:  # decrypt failures, network blips
            raise ExecutionError(f"{type(exc).__name__}: {exc}") from exc
        return {"exchange_tx_id": withdrawal["withdrawal_id"], "amount_executed": amount, "fee": 0.0,
                "order_id": order["order_id"], "tx_type": "withdrawal"}


# --------------------------------------------------------------------------- scheduling math

def add_frequency(dt: datetime, frequency: str) -> Optional[datetime]:
    """Next execution after `dt`, or None for one-time purchases.

    daily +24h, weekly +7d, monthly = same day next month (clamped to the
    month's last day, so the 31st never drifts)."""
    if frequency == "daily":
        return dt + timedelta(days=1)
    if frequency == "weekly":
        return dt + timedelta(days=7)
    if frequency == "monthly":
        year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
        return dt.replace(year=year, month=month, day=min(dt.day, monthrange(year, month)[1]))
    if frequency == "one_time":
        return None
    raise ValueError(f"unknown frequency {frequency!r}")


# --------------------------------------------------------------------------- core

async def find_due_purchases(db: AsyncSession, now: datetime, limit: int = BATCH_LIMIT) -> list[Purchase]:
    """Active purchases due at `now`, oldest due first, with owner + account loaded.

    Rows are taken FOR UPDATE SKIP LOCKED, so a row another transaction is
    already processing is left out instead of waited for or double-processed.
    """
    rows = (await db.execute(
        select(Purchase)
        .options(selectinload(Purchase.user), selectinload(Purchase.exchange_account))
        .where(Purchase.status == "active", Purchase.next_execution.is_not(None), Purchase.next_execution <= now)
        .order_by(Purchase.next_execution).limit(limit)
        .with_for_update(skip_locked=True))).scalars().all()
    return list(rows)


async def execute_one(db: AsyncSession, purchase: Purchase, now: datetime, gateway: ExchangeGateway,
                      dry_run: bool) -> Optional[PurchaseTransaction]:
    """Execute a single due purchase. Returns the transaction, or None when skipped."""
    user: User = purchase.user
    if user is None or user.kyc_status != "verified" or user.suspended:
        logger.info("purchase %s skipped: owner not eligible (kyc=%s suspended=%s)",
                    purchase.id, getattr(user, "kyc_status", None), getattr(user, "suspended", None))
        return None

    tx = PurchaseTransaction(purchase_id=purchase.id, status="pending",
                             tx_type="accumulation" if purchase.purchase_type == "dca" else "withdrawal")
    db.add(tx)
    await db.flush()

    try:
        account = purchase.exchange_account
        if account is None:
            raise ExecutionError("exchange account no longer exists")
        result = await asyncio.to_thread(gateway.execute, account, purchase, dry_run)
        tx.status = "success"
        tx.exchange_tx_id = result.get("exchange_tx_id")
        tx.tx_type = result.get("tx_type", tx.tx_type)
        tx.amount_executed = result.get("amount_executed")
        tx.fee = result.get("fee")
        tx.executed_at = now
        purchase.failure_count = 0
        purchase.last_error = None
        nxt = add_frequency(now, purchase.frequency)
        if nxt is None:
            purchase.status = "completed"
            purchase.next_execution = None
        else:
            purchase.next_execution = nxt
        logger.info("purchase %s executed (%s %s -> %s, dry_run=%s)", purchase.id, tx.amount_executed,
                    purchase.crypto_symbol,
                    purchase.wallet_address if purchase.purchase_type == "autobuy" else "held on exchange", dry_run)
    except ExecutionError as exc:
        tx.status = "failed"
        tx.error_message = str(exc)[:500]
        tx.executed_at = now
        purchase.failure_count = (purchase.failure_count or 0) + 1
        purchase.last_error = tx.error_message
        if purchase.failure_count >= MAX_ATTEMPTS:
            purchase.status = "failed"
            purchase.next_execution = None
            logger.error("purchase %s marked failed after %s attempts: %s", purchase.id, purchase.failure_count, exc)
        else:
            logger.warning("purchase %s attempt %s/%s failed: %s", purchase.id, purchase.failure_count, MAX_ATTEMPTS, exc)
    await db.commit()
    return tx


async def execute_due_purchases(db: AsyncSession, now: Optional[datetime] = None,
                                gateway: Optional[ExchangeGateway] = None,
                                dry_run: Optional[bool] = None) -> dict:
    """Process every due purchase. Returns {"executed", "failed", "skipped"} counts.

    Always logs a "run started" and an "auto-buy run" summary line (even when
    nothing is due) so a silent scheduler is distinguishable from an idle one.
    """
    now = now or utcnow()
    gateway = gateway or ExchangeClientGateway()
    dry_run = settings.autobuy_dry_run if dry_run is None else dry_run
    counts = {"executed": 0, "failed": 0, "skipped": 0}
    started = time.perf_counter()
    logger.info("auto-buy run started (pid=%s now=%s dry_run=%s)", os.getpid(), now.isoformat(), dry_run)

    # Admin pause switch (Redis, shared by every worker, survives restarts).
    # Fails closed: no readable switch -> no run.
    from api.services import scheduler_state
    try:
        pause = await scheduler_state.pause_info()
    except scheduler_state.PauseStateUnavailable as exc:
        logger.error("auto-buy run skipped: cannot read the pause switch from Redis (%s); "
                     "failing closed (pid=%s)", exc, os.getpid())
        return counts
    if pause["paused"]:
        logger.warning("auto-buy run skipped: schedulers PAUSED by admin %s since %s (pid=%s)",
                       pause["by"], pause["since"], os.getpid())
        await scheduler_state.record_run("autobuy", "skipped_paused",
                                         detail=f"paused by {pause['by']} since {pause['since']}")
        return counts

    # The advisory lock lives on its own connection so that the session's
    # per-purchase commits cannot release it part-way through the run.
    async with db.bind.connect() as lock_conn:
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})).scalar()
        if not got:
            logger.info("auto-buy run skipped: another worker holds the run lock (pid=%s)", os.getpid())
            return counts
        try:
            q_start = time.perf_counter()
            due = await find_due_purchases(db, now)
            logger.info("auto-buy run: %d due purchase(s) found in %.0f ms (db connect + query)",
                        len(due), (time.perf_counter() - q_start) * 1000)
            for purchase in due:
                try:
                    tx = await execute_one(db, purchase, now, gateway, dry_run)
                except Exception:  # never let one bad row stop the batch
                    logger.exception("purchase %s raised unexpectedly", purchase.id)
                    await db.rollback()
                    counts["failed"] += 1
                    continue
                if tx is None:
                    counts["skipped"] += 1
                elif tx.status == "success":
                    counts["executed"] += 1
                else:
                    counts["failed"] += 1
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            await lock_conn.commit()
    logger.info("auto-buy run finished in %.0f ms: %s (pid=%s)",
                (time.perf_counter() - started) * 1000, counts, os.getpid())
    # Heartbeat for /health and the admin alerts (only the worker that held
    # the lock reports; a run with failures and no success counts as failed).
    ok = not (counts["failed"] and not counts["executed"])
    await scheduler_state.record_run("autobuy", "ok" if ok else "failed", counts=counts)
    await scheduler_state.bump_failures("autobuy", ok)
    return counts


async def _run_once() -> dict:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await execute_due_purchases(session)
    finally:
        await engine.dispose()


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


def build_scheduler():
    """The APScheduler instance main.py starts: one interval job with a
    generous misfire grace (late wake-ups run instead of being discarded) and
    the miss/skip/error listener attached. Not started."""
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(execute_purchases, "interval", minutes=INTERVAL_MINUTES, id="execute_purchases",
                  max_instances=1, coalesce=True, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    sched.add_listener(job_event_listener, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR)
    return sched


def job_event_listener(event) -> None:
    """APScheduler listener: surface the cases where execute_purchases is NOT
    called, on this module's logger, so they sit next to the run logs.

    Register with EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR.
    """
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
    if getattr(event, "job_id", None) != "execute_purchases":
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
