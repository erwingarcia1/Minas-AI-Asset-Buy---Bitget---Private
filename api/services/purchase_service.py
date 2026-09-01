"""Purchase executor: turns a RecurringPurchase into a (dry-run or live) Binance
spot buy and records a Transaction for every attempt, success or failure."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import APIKey, RecurringPurchase, Transaction, User
from api.routes.exchange import decrypt_value
from api.services.binance_client import BinanceClient, BinanceClientError
from api.services.email_service import email_service

logger = logging.getLogger("autobuy.purchase")


class PurchaseNotFound(Exception):
    """No such purchase for this user (also covers cross-user access)."""


class PurchaseSkipped:
    """Execution intentionally not performed this cycle (price gate / band).
    Not a failure: no transaction is recorded and no alert counter moves."""

    def __init__(self, purchase, reason: str):
        self.purchase = purchase
        self.reason = reason
        self.status = "skipped"


HIGH_VOLATILITY_PCT = 5.0


async def price_gate(purchase) -> str | None:
    """Reason to skip this cycle, or None to proceed. Fail-open on missing
    price data for plain bands, fail-closed for limit orders (a limit order
    must never execute blind)."""
    needs_price = (purchase.limit_price is not None
                   or purchase.min_price is not None
                   or purchase.max_price is not None)
    if not needs_price:
        return None
    import api.services.market_data as market_mod
    price = await market_mod.market_data.get_price(purchase.crypto)
    if price is None:
        if purchase.limit_price is not None:
            return "price unavailable; limit order not evaluated"
        return None  # plain band degrades to a normal DCA buy
    if purchase.limit_price is not None and price > purchase.limit_price:
        return f"price {price} above limit {purchase.limit_price}"
    if purchase.min_price is not None and price < purchase.min_price:
        return f"price {price} below band floor {purchase.min_price}"
    if purchase.max_price is not None and price > purchase.max_price:
        return f"price {price} above band ceiling {purchase.max_price}"
    return None


async def effective_amount(purchase) -> float:
    """Volatility-adaptive DCA: in high 30d volatility, buy half the amount
    (the scheduler halves the interval to match, keeping spend constant).
    Never goes below the 10 USDT venue minimum."""
    if not purchase.volatility_enabled:
        return purchase.amount_usdt
    import api.services.market_data as market_mod
    vol = await market_mod.market_data.get_volatility_30d(purchase.crypto)
    if vol is not None and vol >= HIGH_VOLATILITY_PCT and purchase.amount_usdt / 2 >= 10:
        return purchase.amount_usdt / 2
    return purchase.amount_usdt


class PurchaseService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def execute_purchase(self, user_id: UUID, purchase_id: UUID,
                               dry_run: bool = True,
                               due_date: Optional[datetime] = None) -> Transaction:
        """Execute one purchase. Raises PurchaseNotFound if the purchase does not
        belong to `user_id`; every other failure is recorded as a failed
        Transaction and returned, never raised.

        `due_date` (set by the scheduler) makes execution idempotent: if a
        completed transaction for this purchase already exists at/after it,
        that transaction is returned instead of buying twice.
        """
        purchase = (await self.db.execute(
            select(RecurringPurchase).where(
                RecurringPurchase.id == purchase_id,
                RecurringPurchase.user_id == user_id,
            ))).scalar_one_or_none()
        if purchase is None:
            raise PurchaseNotFound(str(purchase_id))

        if due_date is not None:
            existing = (await self.db.execute(
                select(Transaction).where(
                    Transaction.recurring_purchase_id == purchase.id,
                    Transaction.status == "completed",
                    Transaction.executed_at >= due_date,
                ))).scalars().first()
            if existing is not None:
                logger.info("purchase %s already executed for due date %s (tx %s)",
                            purchase.id, due_date, existing.id)
                return existing

        user = (await self.db.execute(
            select(User).where(User.id == user_id))).scalar_one()

        symbol = f"{purchase.crypto.upper()}USDT"

        async def fail(reason: str) -> Transaction:
            return await self._record(purchase, user_id, symbol, 0.0, purchase.amount_usdt,
                                      None, None, "failed", reason)

        if not purchase.enabled:
            return await fail("purchase is disabled")
        if user.kyc_status != "verified":
            return await fail("KYC not verified")

        key = (await self.db.execute(
            select(APIKey).where(APIKey.user_id == user_id,
                                 APIKey.exchange == "binance"))).scalar_one_or_none()
        if key is None:
            return await fail("no Binance API key on file")
        if not key.verified:
            return await fail("Binance API key not verified — verify it under Exchange settings")

        skip_reason = await price_gate(purchase)
        if skip_reason is not None:
            logger.info("purchase %s skipped: %s", purchase.id, skip_reason)
            return PurchaseSkipped(purchase, skip_reason)

        amount = await effective_amount(purchase)
        client = BinanceClient(decrypt_value(key.encrypted_api_key),
                               decrypt_value(key.encrypted_api_secret))
        client_oid = f"ab-{purchase.id.hex[:12]}-{datetime.now(timezone.utc):%Y%m%d%H%M}"
        try:
            order = await asyncio.to_thread(
                client.execute_order, symbol, amount,
                dry_run, client_oid)
        except BinanceClientError as exc:
            logger.warning("purchase %s failed: %s", purchase.id, exc)
            return await fail(str(exc)[:500])

        logger.info("purchase %s executed: %s %s @ %s (order %s, dry_run=%s)",
                    purchase.id, order["amount_crypto"], purchase.crypto,
                    order["exchange_rate"], order["order_id"], order["dry_run"])
        if purchase.purchase_type in ("one_time", "limit_order"):
            purchase.enabled = False  # one-shot: executed, now done
        return await self._record(purchase, user_id, symbol, order["amount_crypto"],
                                  order["cost_usdt"], order["exchange_rate"],
                                  order["order_id"], "completed", None)

    async def _record(self, purchase, user_id, symbol, amount_crypto, cost_usdt,
                      exchange_rate, order_id, status, error) -> Transaction:
        tx = Transaction(
            user_id=user_id,
            recurring_purchase_id=purchase.id,
            crypto=purchase.crypto.upper(),
            amount_crypto=amount_crypto,
            cost_usdt=cost_usdt,
            exchange_rate=exchange_rate,
            binance_order_id=order_id,
            status=status,
            error_message=error,
            executed_at=datetime.now(timezone.utc),
        )
        self.db.add(tx)
        await self.db.commit()
        await self.db.refresh(tx)
        from api.services.metrics import transactions_total
        transactions_total.labels(status, purchase.crypto.upper()).inc()
        try:
            user = (await self.db.execute(
                select(User).where(User.id == user_id))).scalar_one_or_none()
            if user is not None:
                await email_service.send_purchase_notification(user.email, purchase, tx)
                if status == "completed":
                    from api.services.email_campaigns import after_purchase_completed
                    await after_purchase_completed(self.db, user, tx)
        except Exception:
            logger.exception("purchase notification email failed (tx %s)", tx.id)
        return tx
