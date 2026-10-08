"""Purchase executor: due-purchase selection, fill guard, reconciliation,
idempotent buys, withdrawals, withdrawal polling and the batch driver.

Moved verbatim from api/tasks/purchase_scheduler.py on 2026-09-22 (audit
Batch B3); that path re-exports everything here. Nothing in this file was
reworded: every function body hashes identical to the original.
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
from api.services.exchange_gateway import ExchangeClientGateway, ExchangeGateway, ExecutionError  # noqa: F401

logger = logging.getLogger("autobuy.tasks.purchases")

MAX_ATTEMPTS = 3          # attempts per cycle, one per scheduler tick (5 min apart)


MAX_MISSED_CYCLES = 3     # a recurring purchase that misses this many cycles in a row is stopped (alerted)


BATCH_LIMIT = 200


# Session-level Postgres advisory lock held for the whole run: with one
# scheduler per gunicorn worker, only the first worker to reach a window
# executes; the others log and return. Distinct from the recurring tick's key.
ADVISORY_LOCK_KEY = 0x41425032  # "ABP2"


KEY_REMOVED_MESSAGE = "Exchange key removed. Add an exchange key and create a new purchase."


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


def validate_fill(fill: dict, requested_usd: float, reference_price: Optional[float] = None) -> Optional[str]:
    """Why a fill must NOT be recorded as a completed buy, or None when it is sane.

    A fill is accepted only when it is marked filled, both the amount and the
    cost are positive, the cost is within FILL_TOLERANCE_PCT of the amount
    requested, the amount is within the tolerance of cost / price (price = the
    reference read at execution when available, else the fill's own average),
    and the fill is internally consistent (amount x average ~= cost). Zero,
    partial and implausible fills are all rejected the same way.
    """
    tol = max(float(settings.fill_tolerance_pct), 0.0) / 100.0
    if not fill.get("filled"):
        return f"order is {fill.get('raw_status') or 'unknown'} on the exchange, not filled"
    amount = float(fill.get("amount_crypto") or 0.0)
    cost = float(fill.get("cost_usdt") or 0.0)
    rate = fill.get("exchange_rate")
    rate = float(rate) if rate not in (None, "", 0) else None
    if amount <= 0:
        return f"fill reports amount {amount:g} (status {fill.get('raw_status')})"
    if cost <= 0:
        return f"fill reports cost {cost:g} USDT for {amount:.8f} coins"
    if requested_usd and abs(cost - requested_usd) > tol * requested_usd:
        return (f"cost {cost:.2f} USDT is outside ±{tol * 100:g}% of the {requested_usd:.2f} USDT requested "
                f"(partial fill?)")
    price = reference_price if reference_price and reference_price > 0 else rate
    if price:
        expected = cost / price
        if abs(amount - expected) > tol * expected:
            return (f"amount {amount:.8f} is outside ±{tol * 100:g}% of the {expected:.8f} expected for "
                    f"{cost:.2f} USDT at {price:.8g} ({'reference' if reference_price else 'fill'} price)")
    if rate and abs(amount * rate - cost) > tol * cost:
        return f"fill is inconsistent: {amount:.8f} x {rate:.8g} = {amount * rate:.2f}, cost reported {cost:.2f}"
    return None


def fill_deviation(fill: dict, requested_usd: float, reference_price: Optional[float] = None) -> dict:
    """How far a (valid) fill is from the naive expectation, in percent, with the
    figures behind it: {pct, expected_amount, actual_amount, cost, requested, price}."""
    amount = float(fill.get("amount_crypto") or 0.0)
    cost = float(fill.get("cost_usdt") or 0.0)
    rate = fill.get("exchange_rate")
    price = reference_price if reference_price and reference_price > 0 else (float(rate) if rate else None)
    expected = cost / price if price else amount
    pct = 0.0
    if expected:
        pct = abs(amount - expected) / expected * 100
    if requested_usd:
        pct = max(pct, abs(cost - requested_usd) / requested_usd * 100)
    return {"pct": pct, "expected_amount": expected, "actual_amount": amount, "cost": cost,
            "requested": requested_usd, "price": price}


async def _watch_fill(purchase: Purchase, fill: dict, reference_price: Optional[float]) -> None:
    """Warning band: inside the parking tolerance, outside FILL_WARN_PCT."""
    try:
        d = fill_deviation(fill, float(purchase.amount_usd), reference_price)
        if d["pct"] >= float(settings.fill_warn_pct):
            from api.services import fill_watch
            await fill_watch.record(purchase.id, purchase.user_id, purchase.crypto_symbol, d["pct"], d["expected_amount"],
                                    d["actual_amount"], d["requested"], d["cost"], d["price"])
    except Exception:
        logger.exception("fill deviation watch failed for purchase %s", purchase.id)


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


def next_on_grid(anchor: datetime, frequency: str, now: datetime) -> Optional[datetime]:
    """The first anchor + k × period strictly after `now` (None for one-time).

    Daily/weekly step from the anchor in whole periods, so a late run (the
    scheduler ticks every 5 minutes) never moves the schedule. Monthly keeps
    the anchor's day and clock, clamped to each month's length and computed
    from the anchor every time: 31 Jan -> 28 Feb -> 31 Mar, never stuck on the
    28th. The grid is UTC: around a clock change the Eastern display shifts an
    hour while the UTC time stays put."""
    if frequency == "one_time":
        return None
    if frequency in ("daily", "weekly"):
        step = timedelta(days=1 if frequency == "daily" else 7)
        if now < anchor:
            return anchor
        k = int((now - anchor) // step) + 1
        return anchor + k * step
    if frequency == "monthly":
        months = 0
        while True:
            year = anchor.year + (anchor.month - 1 + months) // 12
            month = (anchor.month - 1 + months) % 12 + 1
            candidate = anchor.replace(year=year, month=month, day=min(anchor.day, monthrange(year, month)[1]))
            if candidate > now:
                return candidate
            months += 1
    raise ValueError(f"unknown frequency {frequency!r}")


def _grid_next(purchase: Purchase, now: datetime) -> Optional[datetime]:
    anchor = purchase.schedule_anchor or purchase.next_execution or now
    return next_on_grid(anchor, purchase.frequency, now)


def _advance(purchase: Purchase, now: datetime) -> None:
    purchase.failure_count = 0
    purchase.last_error = None
    nxt = _grid_next(purchase, now)
    if nxt is None:
        purchase.status = "completed"
        purchase.next_execution = None
    else:
        purchase.next_execution = nxt


def attempt_in_cycle(failure_count: int) -> int:
    """1..MAX_ATTEMPTS: which attempt of the current cycle a failure count is."""
    return ((max(failure_count, 1) - 1) % MAX_ATTEMPTS) + 1


def _fail_attempt(purchase: Purchase, tx: PurchaseTransaction, message: str, now: datetime) -> dict:
    """Record a failed attempt. Attempts within a cycle come one scheduler tick
    apart (next_execution is left in the past). When a cycle's attempts are
    exhausted a recurring purchase MISSES that cycle and moves on to its next
    normal interval — it never stalls silently — until MAX_MISSED_CYCLES in a
    row, when it is stopped (status failed) and alerted. A one-time purchase
    has no next interval and is stopped after its attempts.
    Returns {"attempt", "cycle_exhausted", "missed_cycles", "stopped", "next_execution"} for the event."""
    tx.status = "failed"
    tx.error_message = message[:500]
    tx.executed_at = now
    purchase.failure_count = (purchase.failure_count or 0) + 1
    purchase.last_error = tx.error_message
    attempt = attempt_in_cycle(purchase.failure_count)
    info = {"attempt": attempt, "cycle_exhausted": False, "missed_cycles": purchase.failure_count // MAX_ATTEMPTS,
            "stopped": False, "next_execution": purchase.next_execution}
    if attempt < MAX_ATTEMPTS:
        logger.warning("purchase %s attempt %s/%s failed: %s", purchase.id, attempt, MAX_ATTEMPTS, message)
        return info
    info["cycle_exhausted"] = True
    nxt = _grid_next(purchase, now)   # the missed cycle's successor on the anchor grid, never "now + period"
    if nxt is None or info["missed_cycles"] >= MAX_MISSED_CYCLES:
        purchase.status = "failed"
        purchase.next_execution = None
        info["stopped"] = True
        missed = info["missed_cycles"]
        tail = "" if nxt is None else f" ({missed} cycles missed in a row)"
        purchase.last_error = f"PURCHASE STOPPED after {purchase.failure_count} failed attempts{tail}: {message}"[:500]
        logger.error("purchase %s STOPPED after %s attempts (%s cycle(s) missed): %s", purchase.id,
                     purchase.failure_count, info["missed_cycles"], message)
    else:
        purchase.next_execution = nxt
        info["next_execution"] = nxt
        purchase.last_error = (f"missed this cycle ({info['missed_cycles']} of {MAX_MISSED_CYCLES} in a row) after "
                               f"{MAX_ATTEMPTS} attempts; next attempt {nxt.isoformat()}: {message}")[:500]
        logger.error("purchase %s MISSED a cycle (%s in a row) after %s attempts; next attempt %s: %s",
                     purchase.id, info["missed_cycles"], MAX_ATTEMPTS, nxt.isoformat(), message)
    return info


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
            problem = validate_fill(found["fill"], float(purchase.amount_usd))
            if problem:
                _needs_review(purchase, old, f"earlier order {old.client_oid} filled but the fill is implausible: {problem}", now)
                events.append(("buy_needs_review", problem, old))
                await db.commit()
                return old
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


UNIFIED_REFUSAL_CODE = "40085"   # Bitget: "You are in Unified Account mode, and the Classic Account API is not supported"


def _is_unified_refusal(exc: Exception) -> bool:
    return str(getattr(exc, "code", "") or "") == UNIFIED_REFUSAL_CODE or f"{UNIFIED_REFUSAL_CODE}:" in str(exc)


async def _place_following_account_mode(db: AsyncSession, gateway: ExchangeGateway, account: APIKey, purchase: Purchase,
                                        client_oid: str, dry_run: bool) -> dict:
    """gateway.place_buy, with one twist (v0.8.0): when a Classic-mode key is refused with 40085 the
    account has been migrated to Unified Trading mode, so the key is switched to ``unified`` and the
    SAME attempt (same client order id) is placed once more through the v3 client. No other error
    switches anything; a Classic key on any other error raises exactly as before."""
    try:
        return await asyncio.to_thread(gateway.place_buy, account, purchase, client_oid, dry_run)
    except Exception as exc:
        if account.exchange != "bitget" or (getattr(account, "account_mode", "unknown") or "unknown") == "unified" \
                or not _is_unified_refusal(exc):
            raise
        logger.warning("purchase %s: key %s refused with 40085 (Unified Trading mode); switching the key to unified "
                       "and retrying client_oid=%s through v3", purchase.id, account.id, client_oid)
        account.account_mode = "unified"
        await db.commit()
        return await asyncio.to_thread(gateway.place_buy, account, purchase, client_oid, dry_run)


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

    fail_info: dict = {}   # filled by the last _fail_attempt of this execution

    async def emit_all() -> None:
        try:
            from api.services.purchase_events import PurchaseEvent, emit
            for kind, error, tx in events:
                await emit(db, PurchaseEvent(type=kind, user=user, purchase=purchase, tx=tx, dry_run=dry_run,
                                             error=error, attempt=attempt_in_cycle(purchase.failure_count or 1),
                                             max_attempts=MAX_ATTEMPTS, exchange=exchange_name,
                                             extra=dict(fail_info) if kind == "buy_failed" else {}))
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
        tx.status = "failed"
        tx.error_message = KEY_REMOVED_MESSAGE
        tx.executed_at = now
        purchase.status = "failed"
        purchase.next_execution = None
        purchase.last_error = KEY_REMOVED_MESSAGE
        await db.commit()
        logger.error("purchase %s: exchange key removed; purchase stopped", purchase.id)
        events.append(("buy_failed", tx.error_message, tx))
        await emit_all()
        return tx
    if not account.verified:
        fail_info.update(_fail_attempt(purchase, tx, f"{account.exchange} API key not verified — verify it under Exchange settings", now))
        await db.commit()
        events.append(("buy_failed", tx.error_message, tx))
        await emit_all()
        return tx

    # 3. place the order
    reference_price = None
    try:
        placed = await _place_following_account_mode(db, gateway, account, purchase, tx.client_oid, dry_run)
        tx.order_id = placed.get("order_id")
        reference_price = placed.get("reference_price")
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
            problem = validate_fill(found["fill"], float(purchase.amount_usd), reference_price)
            if problem:
                _needs_review(purchase, tx, f"order {tx.client_oid} filled but the fill is implausible: {problem}", now)
                await db.commit()
                events.append(("buy_needs_review", problem, tx))
                await emit_all()
                return tx
            _record_fill(tx, found["fill"], now)
            _advance(purchase, now)
            await db.commit()
            await _watch_fill(purchase, found["fill"], reference_price)
            events.append(("buy_executed", None, tx))
        elif found.get("state") in ("not_found", "cancelled"):
            fail_info.update(_fail_attempt(purchase, tx, str(exc), now))
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
                fail_info.update(_fail_attempt(purchase, tx, str(exc), now))
                await db.commit()
                events.append(("buy_failed", str(exc), tx))
                await emit_all()
                return tx
            _needs_review(purchase, tx, f"order {tx.order_id} was placed but its fill could not be read: {exc}", now)
            await db.commit()
            events.append(("buy_needs_review", str(exc), tx))
            await emit_all()
            return tx
        problem = validate_fill(fill, float(purchase.amount_usd), reference_price)
        if problem:
            # zero, partial, implausible or simply not filled: a human looks, nothing is recorded as bought
            _needs_review(purchase, tx, f"order {tx.order_id}: {problem}", now)
            await db.commit()
            events.append(("buy_needs_review", problem, tx))
            await emit_all()
            return tx
        _record_fill(tx, fill, now)
        _advance(purchase, now)
        await db.commit()
        await _watch_fill(purchase, fill, reference_price)
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
                             withdrawal_memo=purchase.wallet_memo or None,
                             withdrawal_fee_mode=bitget_market.fee_mode(),   # recorded at send time; readers never recompute under a later setting
                             order_id=buy.order_id, fill_price=buy.fill_price)
    db.add(wd)
    await db.commit()
    if not (buy.amount_executed and buy.amount_executed > 0) or amount <= 0:
        # Never hand an unknown or zero amount to the exchange: park it, no call is made.
        wd.status = "failed"
        wd.executed_at = now
        wd.error_message = (f"withdrawal not attempted: the buy recorded amount {buy.amount_executed!r}"
                            f"{' (nothing left after the trading fee)' if buy.amount_executed and amount <= 0 else ''}")[:500]
        purchase.status = "withdrawal_failed"
        purchase.next_execution = None
        purchase.last_error = wd.error_message
        await db.commit()
        logger.error("purchase %s: %s", purchase.id, wd.error_message)
        events.append(("withdrawal_failed", wd.error_message, wd))
        return wd
    try:
        result = await asyncio.to_thread(gateway.withdraw, account, purchase, amount, wd.client_oid, dry_run)
        wd.status = "success"
        if result.get("size") is not None:
            wd.amount_executed = result["size"]          # what was handed to the exchange
        wd.expected_received = result.get("expected_received", wd.amount_executed)
        wd.withdrawal_id = result.get("withdrawal_id")
        wd.exchange_tx_id = wd.withdrawal_id
        wd.withdrawal_chain = result.get("chain") or wd.withdrawal_chain
        wd.withdrawal_fee = result.get("withdrawal_fee")
        wd.withdrawal_fee_currency = result.get("withdrawal_fee_currency")
        wd.tx_hash = result.get("tx_hash")
        wd.raw_status = result.get("raw_status")
        wd.executed_at = now
        await db.commit()
        logger.info("purchase %s withdrawal %s sent: %s %s -> %s (%s%s), dry_run=%s", purchase.id, wd.id, amount,
                    purchase.crypto_symbol, purchase.wallet_address, wd.withdrawal_chain,
                    f", {'memo' if wd.withdrawal_memo else 'no memo'}", dry_run)
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


def stranded_buy(purchase: Purchase) -> Optional[PurchaseTransaction]:
    """The latest filled buy whose coins never left the exchange: no child
    withdrawal row succeeded. None when there is nothing to retry."""
    txs = list(purchase.transactions or [])
    buys = sorted((t for t in txs if t.tx_type in BUY_TYPES and t.status == "success"),
                  key=lambda t: t.created_at, reverse=True)
    for buy in buys:
        children = [t for t in txs if t.parent_transaction_id == buy.id and t.tx_type == "withdrawal"]
        if children and not any(c.status == "success" for c in children):
            return buy
    return None


async def retry_withdrawal(db: AsyncSession, purchase: Purchase, gateway: Optional[ExchangeGateway] = None,
                           now: Optional[datetime] = None, dry_run: Optional[bool] = None) -> PurchaseTransaction:
    """Send the coins of a stranded buy again. Only the withdrawal is retried —
    place_buy is never called. On success the schedule resumes from now."""
    now = now or utcnow()
    gateway = gateway or ExchangeClientGateway()
    dry_run = settings.autobuy_dry_run if dry_run is None else dry_run
    if purchase.status not in ("withdrawal_failed", "needs_info"):
        raise ValueError(f"purchase is {purchase.status}; only withdrawal_failed / needs_info purchases can retry a withdrawal")
    if not purchase.wallet_address:
        raise ValueError("the purchase has no wallet address to withdraw to")
    buy = stranded_buy(purchase)
    if buy is None:
        raise ValueError("no filled buy with a failed withdrawal to retry")
    account = purchase.exchange_account
    if account is None or not account.verified:
        raise ValueError("the purchase's exchange key is missing or unverified")
    events: list = []
    wd = await _withdraw_after_buy(db, purchase, account, buy, gateway, now, dry_run, events)
    if wd.status == "success":
        purchase.status = "active"
        _advance(purchase, now)   # may complete a one-time purchase
        await db.commit()
        logger.info("purchase %s: withdrawal retried and sent (%s), schedule resumed", purchase.id, wd.id)
    from api.services.purchase_events import PurchaseEvent, emit
    for kind, err, tx in events:
        try:
            await emit(db, PurchaseEvent(type=kind, user=purchase.user, purchase=purchase, tx=tx, dry_run=dry_run,
                                         error=err, exchange=account.exchange))
        except Exception:
            logger.exception("purchase %s: event emission failed", purchase.id)
    return wd


# Withdrawal statuses that need no further polling. "simulated" = dry run, nothing was sent.
WITHDRAWAL_SETTLED = ("success", "wallet_fail", "cancel", "reject", "fail", "failed", "simulated", "unconfirmed")


WITHDRAWAL_POLL_WINDOW = timedelta(days=7)


WITHDRAWAL_POLL_INTERVAL_MINUTES = 10


WITHDRAWAL_LOCK_KEY = 0x41425750  # "ABWP"


async def find_open_withdrawals(db: AsyncSession, now: datetime, limit: int = BATCH_LIMIT) -> list[PurchaseTransaction]:
    """Sent withdrawals (last 7 days) the exchange has not settled yet."""
    since = now - WITHDRAWAL_POLL_WINDOW
    q = (select(PurchaseTransaction)
         .options(selectinload(PurchaseTransaction.purchase).selectinload(Purchase.exchange_account),
                  selectinload(PurchaseTransaction.purchase).selectinload(Purchase.user))
         .where(PurchaseTransaction.tx_type == "withdrawal", PurchaseTransaction.status == "success",
                PurchaseTransaction.executed_at >= since,
                or_(PurchaseTransaction.raw_status.is_(None), PurchaseTransaction.raw_status.notin_(WITHDRAWAL_SETTLED)))
         .order_by(PurchaseTransaction.executed_at.asc()).limit(limit))
    return list((await db.execute(q)).scalars().all())


async def poll_withdrawals(db: AsyncSession, now: Optional[datetime] = None,
                           gateway: Optional[ExchangeGateway] = None) -> dict:
    """Ask the exchange about every unsettled withdrawal: store the on-chain tx
    hash and final fee when it succeeds; when the exchange failed, cancelled or
    rejected it, mark the row failed and park the purchase as withdrawal_failed
    (coins are still on the exchange; the withdrawal can be retried, the buy is
    never repeated). Withdrawals unresolved after the 7-day window are marked
    unconfirmed and flagged on Slack."""
    now = now or utcnow()
    gateway = gateway or ExchangeClientGateway()
    counts = {"confirmed": 0, "failed": 0, "pending": 0, "skipped": 0}
    from api.services import scheduler_state
    try:
        pause = await scheduler_state.pause_info()
    except scheduler_state.PauseStateUnavailable as exc:
        logger.error("withdrawal poll skipped: cannot read the pause switch (%s); failing closed", exc)
        return counts
    if pause["paused"]:
        await scheduler_state.record_run("withdrawals", "skipped_paused", detail=f"paused by {pause['by']}")
        return counts
    async with db.bind.connect() as lock_conn:
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": WITHDRAWAL_LOCK_KEY})).scalar()
        if not got:
            return counts
        try:
            for wd in await find_open_withdrawals(db, now):
                purchase = wd.purchase
                account = purchase.exchange_account if purchase else None
                if account is None or account.exchange != "bitget":
                    counts["skipped"] += 1
                    continue
                try:
                    info = await asyncio.to_thread(gateway.withdrawal_status, account, purchase, wd)
                except Exception as exc:
                    logger.warning("withdrawal %s: status lookup failed: %s", wd.id, exc)
                    counts["pending"] += 1
                    continue
                if info.get("found"):
                    wd.raw_status = info.get("raw_status") or wd.raw_status
                    wd.tx_hash = info.get("tx_hash") or wd.tx_hash
                    if info.get("fee") is not None:
                        wd.withdrawal_fee = info["fee"]
                if info.get("done") and info.get("ok"):
                    counts["confirmed"] += 1
                    await db.commit()
                    logger.info("withdrawal %s confirmed by the exchange: tx %s", wd.id, wd.tx_hash)
                    from api.services import telegram
                    await telegram.notify_withdrawal_confirmed(purchase, wd, settings.autobuy_dry_run)   # never raises
                elif info.get("done"):
                    wd.status = "failed"
                    wd.error_message = f"exchange reported the withdrawal as {wd.raw_status}"[:500]
                    purchase.status = "withdrawal_failed"
                    purchase.next_execution = None
                    purchase.last_error = wd.error_message
                    await db.commit()
                    counts["failed"] += 1
                    logger.error("withdrawal %s %s on the exchange; purchase %s parked as withdrawal_failed, no re-buy",
                                 wd.id, wd.raw_status, purchase.id)
                    try:
                        from api.services.purchase_events import PurchaseEvent, emit
                        await emit(db, PurchaseEvent(type="withdrawal_failed", user=purchase.user, purchase=purchase, tx=wd,
                                                     dry_run=settings.autobuy_dry_run, error=wd.error_message,
                                                     exchange=account.exchange))
                    except Exception:
                        logger.exception("withdrawal %s: event emission failed", wd.id)
                elif (wd.executed_at or now) < now - WITHDRAWAL_POLL_WINDOW + timedelta(hours=1):
                    wd.raw_status = "unconfirmed"
                    await db.commit()
                    counts["pending"] += 1
                    from api.services.slack_service import send_slack
                    await send_slack(f":warning: AutoBuy withdrawal {wd.id} (purchase {purchase.id}, {purchase.crypto_symbol}) "
                                     f"still unconfirmed after 7 days — check it on {account.exchange}")
                else:
                    await db.commit()
                    counts["pending"] += 1
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": WITHDRAWAL_LOCK_KEY})
            await lock_conn.commit()
    await scheduler_state.record_run("withdrawals", "ok" if not counts["failed"] else "failed", counts=counts)
    logger.info("withdrawal poll finished: %s", counts)
    return counts


async def _slack_run_summary(failures: list, dry_run: bool) -> None:
    """One Slack line per run for failed buy attempts (per-attempt lines would flood a
    channel during an exchange outage). Stopped purchases get their own line from the events."""
    try:
        from api.services.slack_service import send_slack
        first_p, first_tx = failures[0]
        errors = {}
        for _, tx in failures:
            key = (tx.error_message or "")[:60]
            errors[key] = errors.get(key, 0) + 1
        top = ", ".join(f"{n}x {e!r}" for e, n in sorted(errors.items(), key=lambda kv: -kv[1])[:3])
        await send_slack(f":x: AutoBuy run{' (dry run)' if dry_run else ''}: {len(failures)} purchase(s) failed a buy attempt "
                         f"and will retry next tick — {top}")
    except Exception:
        logger.exception("run failure summary could not be sent")


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
            run_failures: list = []   # (purchase, tx) for the one-line Slack summary
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
                    if tx.tx_type == "buy" and tx.status == "failed":
                        run_failures.append((purchase, tx))
            if run_failures:
                await _slack_run_summary(run_failures, dry_run)
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
