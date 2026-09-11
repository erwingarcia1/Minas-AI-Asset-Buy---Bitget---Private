"""Auto-Buy execution job: every 5 minutes, execute due purchases.

For each ``Purchase`` with status 'active' and next_execution <= now whose
owner is KYC-verified and not suspended:

1. settle any earlier buy attempt whose outcome is unknown by looking its
   client order id up on the exchange (a fill is recorded, never re-bought;
   an unresolvable state parks the purchase as ``needs_review``),
2. write the buy row with a unique client order id BEFORE sending,
3. market-buy `amount_usd` worth of the coin and read the fill (base and quote
   volume, average price, fees) from the exchange's order detail,
4. ``autobuy``: send the coins to the wallet as a separate ``withdrawal`` row;
   a failed withdrawal parks the purchase as ``withdrawal_failed`` /
   ``needs_info`` — coins stay on the exchange and only the withdrawal may be
   retried. ``dca``: nothing more; the coins accumulate on the exchange.
   Both steps are simulated when AUTOBUY_DRY_RUN=1 (default): dry runs price
   at the live best ask and never call the order or withdrawal endpoints,
5. on success advance ``next_execution`` by the frequency (one-time purchases
   complete); on a definite buy failure count the attempt and mark the
   purchase 'failed' after MAX_ATTEMPTS consecutive failures.

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
import uuid
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Optional, Protocol

from sqlalchemy import func, select, text
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
    """A failed attempt; the message is stored on the transaction.

    ``stage`` says which step failed ("buy" or "withdraw"); when the buy had
    already filled, ``fill`` carries its details so they are recorded and a
    ``withdrawal_failed`` event (not ``buy_failed``) is emitted."""

    def __init__(self, message: str, stage: str = "buy", fill: Optional[dict] = None, code: Optional[str] = None):
        super().__init__(message)
        self.stage = stage
        self.fill = fill
        self.code = code

    @property
    def needs_info(self) -> bool:
        """Bitget rejected a withdrawal for missing travel-rule / identity data."""
        m = str(self).lower()
        return any(k in m for k in ("travel", "identity", "kyt", "beneficiary", "first name", "last name",
                                    "areacode", "area code", "membercode", "compliance"))


class ExchangeGateway(Protocol):
    """What the executor needs from an exchange; production uses ExchangeClientGateway,
    tests substitute a fake. Every method raises ExecutionError on venue failures."""

    def place_buy(self, account: APIKey, purchase: Purchase, client_oid: str, dry_run: bool) -> dict:
        """Send the market buy tagged with our client id. Returns {"order_id"}."""
        ...

    def fetch_fill(self, account: APIKey, purchase: Purchase, order_id: str, client_oid: str, dry_run: bool) -> dict:
        """{order_id, filled, raw_status, amount_crypto, exchange_rate, cost_usdt, fee, fee_currency}."""
        ...

    def lookup_order(self, account: APIKey, purchase: Purchase, client_oid: str) -> dict:
        """{"state": filled | open | cancelled | not_found, "fill": {...} | None} for a client id."""
        ...

    def withdraw(self, account: APIKey, purchase: Purchase, amount: float, client_oid: str, dry_run: bool) -> dict:
        """{withdrawal_id, chain, withdrawal_fee, withdrawal_fee_currency, tx_hash, raw_status}."""
        ...


def _client_for(account: APIKey):
    from api.routes.exchange import decrypt_value
    from api.services.binance_client import ExchangeClient
    return ExchangeClient(decrypt_value(account.encrypted_api_key),
                          decrypt_value(account.encrypted_api_secret),
                          decrypt_value(account.encrypted_passphrase) if account.encrypted_passphrase else "",
                          exchange=account.exchange)


class ExchangeClientGateway:
    """Buys, reads fills and withdraws through the platform's venue-agnostic ExchangeClient."""

    @staticmethod
    def _wrap(exc: Exception, stage: str) -> ExecutionError:
        from api.services.binance_client import BinanceClientError
        if isinstance(exc, ExecutionError):
            return exc
        if isinstance(exc, BinanceClientError):
            return ExecutionError(f"{exc.code}: {exc}", stage=stage, code=str(exc.code))
        return ExecutionError(f"{type(exc).__name__}: {exc}", stage=stage, code=type(exc).__name__)

    def place_buy(self, account, purchase, client_oid, dry_run):
        try:
            if account.exchange == "bitget":
                # pair exists, is online, order above Bitget's minimum — from the hour-cached symbol data
                from api.services import bitget_market
                bitget_market.check_symbol(purchase.crypto_symbol, purchase.amount_usd,
                                           bitget_market.get_symbol_sync(purchase.crypto_symbol))
            client = _client_for(account)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            return client.place_buy(symbol, purchase.amount_usd, client_oid, dry_run=dry_run)
        except Exception as exc:
            raise self._wrap(exc, "buy") from exc

    def fetch_fill(self, account, purchase, order_id, client_oid, dry_run):
        try:
            client = _client_for(account)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            return client.fetch_fill(symbol, purchase.amount_usd, order_id, client_oid, dry_run=dry_run)
        except Exception as exc:
            raise self._wrap(exc, "fill") from exc

    def lookup_order(self, account, purchase, client_oid):
        try:
            client = _client_for(account)
            symbol = client.spot_symbol(purchase.crypto_symbol, "USDT")
            return client.lookup_order(symbol, client_oid)
        except Exception as exc:
            raise self._wrap(exc, "lookup") from exc

    def withdraw(self, account, purchase, amount, client_oid, dry_run):
        """Send `amount` of the coin to the purchase's wallet on its chain.

        Bitget: the chain's live fee/minimum/precision (hour-cached) decide the
        withdrawal size — floored to the chain precision — and a size that does
        not clear minimum + fee fails before anything is sent. Dry runs use the
        same real data and never call the withdrawal endpoint.
        """
        if not purchase.wallet_address:
            raise ExecutionError("no wallet address on an autobuy purchase", stage="withdraw")
        chain = purchase.blockchain_network
        fee = fee_currency = None
        size = amount
        try:
            if account.exchange == "bitget":
                from api.services import bitget_market
                entry = bitget_market.find_chain(bitget_market.get_chains_sync(purchase.crypto_symbol), chain)
                if entry is None:
                    raise ExecutionError(f"Bitget lists no chain {chain!r} for {purchase.crypto_symbol}",
                                         stage="withdraw", code="CHAIN_NOT_AVAILABLE")
                if not entry["withdrawable"]:
                    raise ExecutionError(f"Bitget has {purchase.crypto_symbol} withdrawals on {chain} suspended",
                                         stage="withdraw", code="CHAIN_NOT_WITHDRAWABLE")
                factor = 10 ** int(entry["withdraw_min_scale"])
                size = int(amount * factor) / factor
                fee = (entry["withdraw_fee"] or 0.0) + size * (entry["extra_withdraw_fee_pct"] or 0.0)
                fee_currency = purchase.crypto_symbol
                if size < (entry["min_withdraw_amount"] or 0.0) + fee or size - fee <= 0:
                    raise ExecutionError(
                        f"{size:.8f} {purchase.crypto_symbol} is below Bitget's minimum withdrawal on {chain} "
                        f"({entry['min_withdraw_amount']:g} + {fee:.8f} fee)", stage="withdraw", code="WITHDRAW_BELOW_MINIMUM")
            client = _client_for(account)
            result = client.withdraw(purchase.crypto_symbol, purchase.wallet_address, size,
                                     network=chain if account.exchange == "bitget"
                                     else WITHDRAW_NETWORK_CODES.get(purchase.crypto_symbol, chain),
                                     client_oid=client_oid, dry_run=dry_run)
        except Exception as exc:
            raise self._wrap(exc, "withdraw") from exc
        return {"withdrawal_id": result["withdrawal_id"], "chain": chain, "size": size,
                "withdrawal_fee": result.get("fee", fee), "withdrawal_fee_currency": result.get("fee_currency", fee_currency),
                "tx_hash": result.get("tx_hash"), "raw_status": "simulated" if dry_run else "submitted"}


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
        .options(selectinload(Purchase.user), selectinload(Purchase.exchange_account),
                 selectinload(Purchase.transactions))
        .where(Purchase.status == "active", Purchase.next_execution.is_not(None), Purchase.next_execution <= now)
        .order_by(Purchase.next_execution).limit(limit)
        .with_for_update(skip_locked=True))).scalars().all()
    return list(rows)


def _record_fill(tx: PurchaseTransaction, fill: dict, now: datetime) -> None:
    tx.status = "success"
    tx.raw_status = fill.get("raw_status")
    tx.order_id = fill.get("order_id") or tx.order_id
    tx.exchange_tx_id = tx.exchange_tx_id or tx.order_id
    tx.amount_executed = fill.get("amount_crypto")
    tx.fill_price = fill.get("exchange_rate")
    tx.cost_usd = fill.get("cost_usdt")
    tx.fee = fill.get("fee")
    tx.fee_currency = fill.get("fee_currency")
    tx.executed_at = now


def _advance(purchase: Purchase, now: datetime) -> None:
    purchase.failure_count = 0
    purchase.last_error = None
    nxt = add_frequency(now, purchase.frequency)
    if nxt is None:
        purchase.status = "completed"
        purchase.next_execution = None
    else:
        purchase.next_execution = nxt


def _fail_attempt(purchase: Purchase, tx: PurchaseTransaction, message: str, now: datetime) -> None:
    tx.status = "failed"
    tx.error_message = message[:500]
    tx.executed_at = now
    purchase.failure_count = (purchase.failure_count or 0) + 1
    purchase.last_error = tx.error_message
    if purchase.failure_count >= MAX_ATTEMPTS:
        purchase.status = "failed"
        purchase.next_execution = None
        logger.error("purchase %s marked failed after %s attempts: %s", purchase.id, purchase.failure_count, message)
    else:
        logger.warning("purchase %s attempt %s/%s failed: %s", purchase.id, purchase.failure_count, MAX_ATTEMPTS, message)


def _needs_review(purchase: Purchase, tx: PurchaseTransaction, message: str, now: datetime) -> None:
    """Outcome unknown: stop the purchase and hand it to a human. Never retried automatically."""
    tx.status = "needs_review"
    tx.error_message = message[:500]
    tx.executed_at = now
    purchase.status = "needs_review"
    purchase.next_execution = None
    purchase.last_error = tx.error_message
    logger.error("purchase %s NEEDS REVIEW (attempt %s, client_oid=%s): %s", purchase.id, tx.id, tx.client_oid, message)


async def _reconcile_unresolved(db: AsyncSession, purchase: Purchase, account: APIKey, gateway: ExchangeGateway,
                                now: datetime, events: list) -> Optional[PurchaseTransaction]:
    """Before any new buy: settle every earlier buy attempt whose outcome is unknown.

    Returns a transaction when it resolved to a FILL (the caller must not buy
    again) or when the purchase was parked for review; None when every earlier
    attempt is settled as never-sent and a new buy may proceed.
    """
    unresolved = [t for t in (purchase.transactions or [])
                  if t.tx_type == "buy" and t.status == "pending" and t.client_oid]
    for old in sorted(unresolved, key=lambda t: t.created_at):
        try:
            found = await asyncio.to_thread(gateway.lookup_order, account, purchase, old.client_oid)
        except Exception as exc:
            _needs_review(purchase, old, f"could not confirm earlier order {old.client_oid} on the exchange: {exc}", now)
            events.append(("buy_needs_review", str(exc), old))
            await db.commit()
            return old
        state = found.get("state")
        if state == "filled":
            logger.warning("purchase %s: earlier attempt %s (client_oid=%s) had FILLED on the exchange; "
                           "recording it, not buying again", purchase.id, old.id, old.client_oid)
            _record_fill(old, found["fill"], now)
            _advance(purchase, now)
            await db.commit()
            events.append(("buy_executed", None, old))
            return old
        if state in ("not_found", "cancelled"):
            old.status = "failed"
            old.raw_status = state
            old.error_message = (old.error_message or "")[:300] + f" [reconciled: order {state} on the exchange]"
            old.executed_at = old.executed_at or now
            await db.commit()
            continue
        _needs_review(purchase, old, f"earlier order {old.client_oid} is still {state} on the exchange", now)
        events.append(("buy_needs_review", f"order state {state}", old))
        await db.commit()
        return old
    return None


async def execute_one(db: AsyncSession, purchase: Purchase, now: datetime, gateway: ExchangeGateway,
                      dry_run: bool) -> Optional[PurchaseTransaction]:
    """Execute a single due purchase. Returns the buy transaction, or None when skipped.

    Order of operations (idempotent buys):
    1. reconcile earlier attempts with an unknown outcome — a fill is recorded
       and never re-bought; an unknown state parks the purchase as needs_review;
    2. create the buy row with a unique client_oid and COMMIT it before sending;
    3. place the order; on any error look the client_oid up before deciding;
    4. read the fill from order detail; if that is impossible the attempt is
       needs_review, never retried;
    5. autobuy: create a separate withdrawal row and send the withdrawal; a
       failed withdrawal parks the purchase as withdrawal_failed (coins kept,
       never re-bought; only the withdrawal may be retried).
    """
    user: User = purchase.user
    if user is None or user.kyc_status != "verified" or user.suspended:
        logger.info("purchase %s skipped: owner not eligible (kyc=%s suspended=%s)",
                    purchase.id, getattr(user, "kyc_status", None), getattr(user, "suspended", None))
        return None
    account = purchase.exchange_account
    exchange_name = account.exchange if account is not None else None
    events: list = []   # (event type, error, tx) emitted after the commit

    async def emit_all() -> None:
        try:
            from api.services.purchase_events import PurchaseEvent, emit
            for kind, error, tx in events:
                await emit(db, PurchaseEvent(type=kind, user=user, purchase=purchase, tx=tx, dry_run=dry_run,
                                             error=error, attempt=purchase.failure_count or 1,
                                             max_attempts=MAX_ATTEMPTS, exchange=exchange_name))
        except Exception:
            logger.exception("purchase %s: event emission failed", purchase.id)

    # 1. earlier attempts first
    if account is not None:
        settled = await _reconcile_unresolved(db, purchase, account, gateway, now, events)
        if settled is not None:
            if settled.status == "success" and purchase.purchase_type == "autobuy":
                await _withdraw_after_buy(db, purchase, account, settled, gateway, now, dry_run, events)
            await emit_all()
            return settled

    # 2. the buy row exists (with its client id) before anything reaches the exchange
    tx = PurchaseTransaction(purchase_id=purchase.id, status="pending", tx_type="buy",
                             client_oid=f"ab-{uuid.uuid4().hex[:24]}")
    db.add(tx)
    await db.commit()

    if account is None:
        _fail_attempt(purchase, tx, "exchange account no longer exists", now)
        await db.commit()
        events.append(("buy_failed", tx.error_message, tx))
        await emit_all()
        return tx
    if not account.verified:
        _fail_attempt(purchase, tx, f"{account.exchange} API key not verified — verify it under Exchange settings", now)
        await db.commit()
        events.append(("buy_failed", tx.error_message, tx))
        await emit_all()
        return tx

    # 3. place the order
    try:
        placed = await asyncio.to_thread(gateway.place_buy, account, purchase, tx.client_oid, dry_run)
        tx.order_id = placed.get("order_id")
        await db.commit()
    except Exception as exc:
        # The order may or may not have reached the exchange: ask before deciding.
        try:
            found = await asyncio.to_thread(gateway.lookup_order, account, purchase, tx.client_oid)
        except Exception as lookup_exc:
            _needs_review(purchase, tx, f"buy raised ({exc}) and the order could not be looked up: {lookup_exc}", now)
            await db.commit()
            events.append(("buy_needs_review", str(exc), tx))
            await emit_all()
            return tx
        if found.get("state") == "filled":
            logger.warning("purchase %s: buy raised (%s) but order %s had filled; recording, not re-buying",
                           purchase.id, exc, tx.client_oid)
            _record_fill(tx, found["fill"], now)
            _advance(purchase, now)
            await db.commit()
            events.append(("buy_executed", None, tx))
        elif found.get("state") in ("not_found", "cancelled"):
            _fail_attempt(purchase, tx, str(exc), now)
            await db.commit()
            events.append(("buy_failed", str(exc), tx))
            await emit_all()
            return tx
        else:
            _needs_review(purchase, tx, f"buy raised ({exc}); the order is {found.get('state')} on the exchange", now)
            await db.commit()
            events.append(("buy_needs_review", str(exc), tx))
            await emit_all()
            return tx
    else:
        # 4. read the fill
        try:
            fill = await asyncio.to_thread(gateway.fetch_fill, account, purchase, tx.order_id, tx.client_oid, dry_run)
        except Exception as exc:
            if dry_run:   # nothing was sent: a simulated fill that cannot be priced is an ordinary failure
                _fail_attempt(purchase, tx, str(exc), now)
                await db.commit()
                events.append(("buy_failed", str(exc), tx))
                await emit_all()
                return tx
            _needs_review(purchase, tx, f"order {tx.order_id} was placed but its fill could not be read: {exc}", now)
            await db.commit()
            events.append(("buy_needs_review", str(exc), tx))
            await emit_all()
            return tx
        if not fill.get("filled"):
            _needs_review(purchase, tx, f"order {tx.order_id} is {fill.get('raw_status')} on the exchange, not filled", now)
            await db.commit()
            events.append(("buy_needs_review", fill.get("raw_status"), tx))
            await emit_all()
            return tx
        _record_fill(tx, fill, now)
        _advance(purchase, now)
        await db.commit()
        logger.info("purchase %s executed (%s %s @ %s, fee %s %s, order %s, dry_run=%s)", purchase.id,
                    tx.amount_executed, purchase.crypto_symbol, tx.fill_price, tx.fee, tx.fee_currency,
                    tx.order_id, dry_run)
        events.append(("buy_executed", None, tx))

    # 5. withdrawal
    if purchase.purchase_type == "autobuy":
        await _withdraw_after_buy(db, purchase, account, tx, gateway, now, dry_run, events)
    await emit_all()
    return tx


async def _withdraw_after_buy(db: AsyncSession, purchase: Purchase, account: APIKey, buy: PurchaseTransaction,
                              gateway: ExchangeGateway, now: datetime, dry_run: bool, events: list) -> PurchaseTransaction:
    """Send the bought coins to the wallet as a separate row. A failure never
    triggers another buy: the purchase is parked as withdrawal_failed (or
    needs_info) and only the withdrawal may be retried."""
    amount = float(buy.amount_executed or 0)
    if buy.fee and (buy.fee_currency or "").upper() == purchase.crypto_symbol.upper():
        amount = max(amount - float(buy.fee), 0.0)   # the venue took its fee in the base coin
    wd = PurchaseTransaction(purchase_id=purchase.id, status="pending", tx_type="withdrawal",
                             parent_transaction_id=buy.id, client_oid=f"abw-{uuid.uuid4().hex[:24]}",
                             amount_executed=amount, withdrawal_chain=purchase.blockchain_network,
                             order_id=buy.order_id, fill_price=buy.fill_price)
    db.add(wd)
    await db.commit()
    try:
        result = await asyncio.to_thread(gateway.withdraw, account, purchase, amount, wd.client_oid, dry_run)
        wd.status = "success"
        if result.get("size") is not None:
            wd.amount_executed = result["size"]
        wd.withdrawal_id = result.get("withdrawal_id")
        wd.exchange_tx_id = wd.withdrawal_id
        wd.withdrawal_chain = result.get("chain") or wd.withdrawal_chain
        wd.withdrawal_fee = result.get("withdrawal_fee")
        wd.withdrawal_fee_currency = result.get("withdrawal_fee_currency")
        wd.tx_hash = result.get("tx_hash")
        wd.raw_status = result.get("raw_status")
        wd.executed_at = now
        await db.commit()
        logger.info("purchase %s withdrawal %s sent: %s %s -> %s (%s), dry_run=%s", purchase.id, wd.id, amount,
                    purchase.crypto_symbol, purchase.wallet_address, wd.withdrawal_chain, dry_run)
        events.append(("withdrawal_sent", None, wd))
    except Exception as exc:
        wd.executed_at = now
        wd.error_message = str(exc)[:500]
        needs_info = isinstance(exc, ExecutionError) and exc.needs_info
        wd.status = "needs_info" if needs_info else "failed"
        purchase.status = "needs_info" if needs_info else "withdrawal_failed"
        purchase.next_execution = None
        purchase.last_error = wd.error_message
        await db.commit()
        logger.error("purchase %s: buy %s filled but withdrawal %s %s: %s — coins kept on the exchange, no re-buy",
                     purchase.id, buy.id, wd.id, wd.status, exc)
        events.append(("withdrawal_needs_info" if needs_info else "withdrawal_failed", str(exc), wd))
    return wd


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
            from api.services.metrics import active_purchases_total
            active_purchases_total.set((await db.execute(
                select(func.count(Purchase.id)).where(Purchase.status == "active"))).scalar_one())
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
    await _register_run_result(ok, started)
    return counts


FAILURE_ALERT_THRESHOLD = 5


async def _register_run_result(ok: bool, started: float) -> None:
    """Consecutive-failure counter (Redis + Prometheus gauge) and the CRITICAL
    alert (log + Slack) when it reaches FAILURE_ALERT_THRESHOLD."""
    from api.services import scheduler_state
    from api.services.metrics import scheduler_consecutive_failures, scheduler_job_duration_seconds
    scheduler_job_duration_seconds.labels("execute_purchases").observe(time.perf_counter() - started)
    fails = await scheduler_state.bump_failures("autobuy", ok)
    if fails >= 0:
        scheduler_consecutive_failures.set(fails)
    if fails == FAILURE_ALERT_THRESHOLD:
        logger.critical("ALERT: %d consecutive Auto-Buy runs failed every due purchase -- check exchange "
                        "connectivity, key validity, balances and withdrawal permissions", fails)
        try:
            from api.services.slack_service import send_slack
            await send_slack(f":rotating_light: AutoBuy: {fails} consecutive execute_purchases runs failed "
                             "every due purchase — see the runbook (docs/ALERTING.md)")
        except Exception:
            logger.exception("slack alert failed")


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


# Session-level advisory lock for the weekly digest: one worker sends, the
# other three find the lock taken and return.
DIGEST_LOCK_KEY = 0x41425744  # "ABWD"


async def _send_weekly_digest_once() -> bool:
    from api.services.email_campaigns import send_weekly_summaries
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
