"""Events emitted by the Auto-Buy executor, and everything that listens.

The executor calls :func:`emit` once per outcome of an attempt:

* ``buy_executed``     – the market buy filled (order id, fill price, cost)
* ``buy_failed``       – the buy did not fill
* ``withdrawal_sent``  – autobuy only: the exchange accepted the withdrawal
* ``withdrawal_failed``– autobuy only: the buy filled but the withdrawal failed
* ``buy_needs_review`` – the buy's outcome could not be confirmed; ops alert, no retry
* ``withdrawal_needs_info`` – the venue wants travel-rule / identity data first

Subscribers (all best effort, each isolated by try/except so a broken email
or webhook can never affect execution or another subscriber):

1. purchase notification email  – one email per attempt outcome
2. lifecycle campaigns          – first-purchase / milestone emails
3. notification dispatcher      – SMS / push / audit (email already sent)
4. Slack ops alert              – failures, and live (non dry-run) withdrawals
5. outbound user webhooks       – ``purchase.<event>`` signed POSTs
6. metrics                      – ``transactions_total`` counter
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from api.models import Purchase, PurchaseTransaction, User
from api.services.history import fmt_amount, presented_status

logger = logging.getLogger("autobuy.events")

EVENTS = ("buy_executed", "buy_failed", "buy_needs_review", "withdrawal_sent", "withdrawal_failed",
          "withdrawal_needs_info")
FAILURE_EVENTS = ("buy_failed", "withdrawal_failed", "withdrawal_needs_info")
OPS_EVENTS = ("buy_needs_review",)   # a human must look; the user is not emailed


@dataclass
class PurchaseEvent:
    type: str
    user: User
    purchase: Purchase
    tx: PurchaseTransaction
    dry_run: bool = True
    error: Optional[str] = None
    attempt: int = 1
    max_attempts: int = 3
    exchange: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def payload(self) -> dict:
        """What webhooks receive."""
        tx = self.tx
        return {
            "event": self.type, "transaction_id": str(tx.id), "purchase_id": str(self.purchase.id),
            "crypto": self.purchase.crypto_symbol, "purchase_type": self.purchase.purchase_type,
            "tx_type": tx.tx_type, "status": presented_status(tx), "amount_crypto": tx.amount_executed,
            "cost_usd": tx.cost_usd, "fill_price": tx.fill_price, "fee": tx.fee, "fee_currency": tx.fee_currency,
            "exchange": self.exchange, "order_id": tx.order_id, "withdrawal_id": tx.withdrawal_id,
            "wallet_address": self.purchase.wallet_address if tx.tx_type == "withdrawal" else None,
            "blockchain_network": self.purchase.blockchain_network if tx.tx_type == "withdrawal" else None,
            "tx_hash": tx.tx_hash, "error": self.error, "dry_run": self.dry_run,
            "executed_at": tx.executed_at.isoformat() if tx.executed_at else None,
        }


Subscriber = Callable[[object, PurchaseEvent], Awaitable[None]]
_subscribers: List[Subscriber] = []


def subscribe(fn: Subscriber) -> Subscriber:
    _subscribers.append(fn)
    return fn


async def emit(db, event: PurchaseEvent) -> None:
    """Fan the event out. Never raises."""
    if event.type not in EVENTS:
        raise ValueError(f"unknown purchase event {event.type!r}")
    logger.info("purchase event %s: purchase=%s tx=%s user=%s dry_run=%s",
                event.type, event.purchase.id, event.tx.id, event.user.id, event.dry_run)
    for fn in list(_subscribers):
        try:
            await fn(db, event)
        except Exception:
            logger.exception("purchase event subscriber %s failed for %s", getattr(fn, "__name__", fn), event.type)


# ------------------------------------------------------------------ subscribers

@subscribe
async def _notification_email(db, event: PurchaseEvent) -> None:
    from api.services.email_service import email_service
    # autobuy: the buy email would be followed by the withdrawal email seconds later;
    # send one email per attempt, on the withdrawal outcome. Review cases go to ops only.
    if event.type in OPS_EVENTS:
        return
    if event.type == "buy_executed" and event.purchase.purchase_type == "autobuy":
        return
    await email_service.send_purchase_event_email(event.user.email, event)


@subscribe
async def _lifecycle_campaigns(db, event: PurchaseEvent) -> None:
    if event.type != "buy_executed":
        return
    from api.services.email_campaigns import on_buy_executed
    await on_buy_executed(db, event.user, event.tx, event.purchase)


@subscribe
async def _dispatcher(db, event: PurchaseEvent) -> None:
    from api.services.notifications import LARGE_TRANSACTION_USDT, dispatcher
    p, tx = event.purchase, event.tx
    if event.type == "buy_executed" and p.purchase_type == "autobuy":
        return  # the withdrawal outcome carries the notification
    if event.type in OPS_EVENTS:
        return
    if event.type in ("buy_executed", "withdrawal_sent"):
        await dispatcher.notify(
            db, event.user, template="purchase_executed",
            subject=f"Bought {p.crypto_symbol}",
            body=f"{fmt_amount(tx.amount_executed or 0)} {p.crypto_symbol} for {(tx.cost_usd or p.amount_usd):.2f} USD"
                 + (f", withdrawal to {p.wallet_address} simulated (nothing was sent)" if event.type == "withdrawal_sent" and presented_status(tx) == "simulated"
                    else f", sent to {p.wallet_address}" if event.type == "withdrawal_sent" else ", held on the exchange"),
            critical=(tx.cost_usd or p.amount_usd) >= LARGE_TRANSACTION_USDT, include_email=False)
    else:
        await dispatcher.notify(
            db, event.user, template="purchase_failed",
            subject=f"{'Withdrawal' if event.type == 'withdrawal_failed' else 'Purchase'} failed: {p.crypto_symbol}",
            body=str(event.error or "")[:200], critical=True, include_email=False)


@subscribe
async def _slack(db, event: PurchaseEvent) -> None:
    from api.services.slack_service import send_slack
    p = event.purchase
    if event.type in OPS_EVENTS:
        await send_slack(f":rotating_light: AutoBuy NEEDS REVIEW: purchase {p.id} ({p.crypto_symbol} {p.amount_usd:.2f} USD "
                         f"via {event.exchange or '?'}, user {event.user.id}) — buy attempt {event.tx.id} "
                         f"client_oid={event.tx.client_oid} order_id={event.tx.order_id or '-'}: "
                         f"{str(event.error or '')[:200]}. Check the exchange before doing anything; "
                         "the purchase will not be retried automatically.")
        return
    if event.type in FAILURE_EVENTS:
        await send_slack(f":x: AutoBuy {event.type.replace('_', ' ')}: {p.crypto_symbol} "
                         f"{p.amount_usd:.2f} USD via {event.exchange or '?'} for user {event.user.id} "
                         f"(attempt {event.attempt}/{event.max_attempts}{', dry run' if event.dry_run else ''}): "
                         f"{str(event.error or '')[:200]}")
    elif event.type == "withdrawal_sent" and not event.dry_run:
        await send_slack(f":outbox_tray: AutoBuy withdrawal sent: {fmt_amount(event.tx.amount_executed or 0)} "
                         f"{p.crypto_symbol} to {p.wallet_address} ({p.blockchain_network}) "
                         f"via {event.exchange or '?'}, withdrawal {event.tx.withdrawal_id}")


@subscribe
async def _webhooks(db, event: PurchaseEvent) -> None:
    from api.services.webhook_service import dispatch
    await dispatch(db, event.user.id, f"purchase.{event.type}", event.payload())


@subscribe
async def _metrics(db, event: PurchaseEvent) -> None:
    from api.services.metrics import transactions_total
    if event.type in ("buy_executed", "buy_failed", "buy_needs_review"):
        label = {"buy_executed": "completed", "buy_failed": "failed", "buy_needs_review": "needs_review"}[event.type]
        transactions_total.labels(label, event.purchase.crypto_symbol).inc()
