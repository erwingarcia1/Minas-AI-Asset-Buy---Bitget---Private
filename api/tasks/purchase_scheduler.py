"""Auto-Buy execution job: every 5 minutes, execute due purchases.

For each ``Purchase`` with status 'active' and next_execution <= now whose
owner is KYC-verified and not suspended:

1. load and decrypt the exchange account credentials,
2. market-buy `amount_usd` worth of the coin, then withdraw the filled amount
   to the purchase's wallet on its network (both simulated when
   AUTOBUY_DRY_RUN=1, the default),
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
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from api.config import settings
from api.database import DATABASE_URL
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow

logger = logging.getLogger("autobuy.tasks.purchases")

MAX_ATTEMPTS = 3
BATCH_LIMIT = 200

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
            withdrawal = client.withdraw(purchase.crypto_symbol, purchase.wallet_address, amount,
                                         network=WITHDRAW_NETWORK_CODES.get(purchase.crypto_symbol),
                                         client_oid=f"abw-{purchase.id.hex[:12]}", dry_run=dry_run)
        except BinanceClientError as exc:
            raise ExecutionError(f"{exc.code}: {exc}") from exc
        except Exception as exc:  # decrypt failures, network blips
            raise ExecutionError(f"{type(exc).__name__}: {exc}") from exc
        return {"exchange_tx_id": withdrawal["withdrawal_id"], "amount_executed": amount, "fee": 0.0,
                "order_id": order["order_id"]}


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
    """Active purchases due at `now`, oldest due first, with owner + account loaded."""
    rows = (await db.execute(
        select(Purchase)
        .options(selectinload(Purchase.user), selectinload(Purchase.exchange_account))
        .where(Purchase.status == "active", Purchase.next_execution.is_not(None), Purchase.next_execution <= now)
        .order_by(Purchase.next_execution).limit(limit))).scalars().all()
    return list(rows)


async def execute_one(db: AsyncSession, purchase: Purchase, now: datetime, gateway: ExchangeGateway,
                      dry_run: bool) -> Optional[PurchaseTransaction]:
    """Execute a single due purchase. Returns the transaction, or None when skipped."""
    user: User = purchase.user
    if user is None or user.kyc_status != "verified" or user.suspended:
        logger.info("purchase %s skipped: owner not eligible (kyc=%s suspended=%s)",
                    purchase.id, getattr(user, "kyc_status", None), getattr(user, "suspended", None))
        return None

    tx = PurchaseTransaction(purchase_id=purchase.id, status="pending")
    db.add(tx)
    await db.flush()

    try:
        account = purchase.exchange_account
        if account is None:
            raise ExecutionError("exchange account no longer exists")
        result = await asyncio.to_thread(gateway.execute, account, purchase, dry_run)
        tx.status = "success"
        tx.exchange_tx_id = result.get("exchange_tx_id")
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
        logger.info("purchase %s executed (%s %s -> %s, dry_run=%s)", purchase.id,
                    tx.amount_executed, purchase.crypto_symbol, purchase.wallet_address, dry_run)
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
    """Process every due purchase. Returns {"executed", "failed", "skipped"} counts."""
    now = now or utcnow()
    gateway = gateway or ExchangeClientGateway()
    dry_run = settings.autobuy_dry_run if dry_run is None else dry_run
    counts = {"executed": 0, "failed": 0, "skipped": 0}
    for purchase in await find_due_purchases(db, now):
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
    if any(counts.values()):
        logger.info("auto-buy run: %s", counts)
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
    try:
        return asyncio.run(_run_once())
    except Exception:
        logger.exception("auto-buy scheduler run failed")
        return {"executed": 0, "failed": 0, "skipped": 0}
