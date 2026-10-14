"""Fire-and-forget email for callers that must never fail on email delivery
(purchase execution, KYC webhooks, campaigns, the notification dispatcher).

A thin wrapper over :mod:`api.integrations.resend`, the platform's only
Resend client: no Resend logic of its own. ``_send`` returns True/False and
never raises; every failure has already been logged on
``autobuy.integrations.resend`` (ERROR with status and reason, or WARNING
when email is not configured). The class is kept so test doubles can
subclass it and override ``_send``.
"""
from __future__ import annotations

from api.config import settings
from api.integrations import resend
from api.integrations.resend import ResendError
import logging

logger = logging.getLogger("autobuy.email")


class EmailService:
    def __init__(self, api_key: str = "", from_email: str = "") -> None:
        # Accepted only for compatibility with existing test doubles. The key
        # and sender come from configuration (RESEND_API_KEY / RESEND_FROM_EMAIL).
        pass

    async def _send(self, to: str, subject: str, html: str, text: str,
                    template: str = "generic") -> bool:
        try:
            await resend.send_email(to, subject, html, text_content=text, template=template)
            return True
        except ResendError:
            return False  # logged by the integration

    async def queue(self, to: str, subject: str, html: str, text: str, template: str = "generic",
                    category: str = "confirmation", user_id=None) -> bool:
        """Off the critical path: push to the outbox (sent by the scheduler with
        retries; `category` failure/security gets more attempts than confirmation).
        If the outbox is unavailable, one short inline attempt; never raises."""
        import asyncio
        from api.services import email_outbox
        if await email_outbox.enqueue(to, subject, html, text, template, category=category,
                                      user_id=str(user_id) if user_id else None):
            return True
        try:
            ok = await asyncio.wait_for(self._send(to, subject, html, text, template), 5.0)
        except Exception as exc:
            ok = False
            logger.error("email lost (outbox unavailable, inline attempt failed): to=%s template=%s reason=%s", to, template, exc)
        if not ok:
            logger.error("email lost (outbox unavailable, inline attempt failed): to=%s template=%s", to, template)
        return ok

    async def send_purchase_event_email(self, user_email: str, event) -> bool:
        """One email per Auto-Buy attempt outcome (see purchase_events). Queued,
        not sent inline: the executor never waits on the email provider."""
        p, tx = event.purchase, event.tx
        coin = p.crypto_symbol
        amount = tx.amount_executed or 0.0
        cost = tx.cost_usd if tx.cost_usd is not None else p.amount_usd
        from api.services.bitget_market import fmt_price
        price = f" at {fmt_price(tx.fill_price, coin)}" if tx.fill_price else ""
        prefix = "[DRY RUN] " if event.dry_run else ""
        from api.services.history import fmt_amount, presented_status
        if event.type == "buy_executed":
            subject = f"{prefix}✓ Purchase complete: {fmt_amount(amount)} {coin}"
            text = (f"You bought {fmt_amount(amount)} {coin} for {cost:.2f} USD{price} on {event.exchange or 'your exchange'}. "
                    f"The coins are held on the exchange (accumulate mode). Order: {tx.order_id or '-'}")
        elif event.type == "withdrawal_sent" and presented_status(tx) == "simulated":
            subject = f"{prefix}✓ Purchase complete: {fmt_amount(amount)} {coin} withdrawal simulated"
            text = (f"You bought {coin} for {cost:.2f} USD{price} on {event.exchange or 'your exchange'}. "
                    f"The withdrawal of {fmt_amount(amount)} {coin} to {p.wallet_address} ({p.blockchain_network}) was "
                    f"SIMULATED (status: simulated) — nothing was sent; the coins stay on the exchange. "
                    f"Order: {tx.order_id or '-'} · Withdrawal: {tx.withdrawal_id or '-'}")
        elif event.type == "withdrawal_sent":
            received = tx.expected_received if tx.expected_received is not None else amount
            subject = f"{prefix}✓ Purchase complete: {fmt_amount(received)} {coin} sent to your wallet"
            text = (f"You bought {coin} for {cost:.2f} USD{price} on {event.exchange or 'your exchange'} "
                    f"and {fmt_amount(amount)} {coin} was sent to {p.wallet_address} ({p.blockchain_network}); "
                    f"about {fmt_amount(received)} {coin} should arrive after the {fmt_amount(tx.withdrawal_fee or 0)} {coin} network fee. "
                    f"Order: {tx.order_id or '-'} · Withdrawal: {tx.withdrawal_id or '-'}")
        elif event.type == "withdrawal_failed":
            subject = f"{prefix}✗ Withdrawal failed: {coin}"
            text = (f"Your {coin} buy filled, but sending {fmt_amount(amount)} {coin} to "
                    f"{p.wallet_address} ({tx.withdrawal_chain or p.blockchain_network}) failed: {event.error}. "
                    f"The coins remain safely on {event.exchange or 'the exchange'} and we will NOT buy again. "
                    "Fix the cause (wallet on the exchange's address book, withdrawal permission, IP allowlist), "
                    "then use 'Retry withdrawal' on the purchase.")
        elif event.type == "withdrawal_needs_info":
            subject = f"{prefix}⚠ Withdrawal needs more information: {coin}"
            text = (f"Your {coin} buy filled, but {event.exchange or 'the exchange'} will not send {fmt_amount(amount)} {coin} to "
                    f"{p.wallet_address} until more information is provided: {event.error}. "
                    f"The coins remain on {event.exchange or 'the exchange'}; nothing will be retried automatically. "
                    "Contact support to add the required details, then retry the withdrawal.")
        elif event.type == "buy_failed":
            # Plain-English wording (#90/#91): raw codes stay on the row and in
            # ops channels; the customer hears what happened, that no money
            # moved, and exactly what to do in Bitget.
            import asyncio
            from api.services import failure_text as ft
            from api.services.low_balance import fmt_run_time
            account = p.exchange_account
            cls = ft.classify(str(event.error or ""))
            ctx = None
            if cls == ft.INSUFFICIENT_BALANCE and not event.dry_run:
                try:
                    ctx = await asyncio.to_thread(ft.read_balance_context, account)
                except Exception:   # noqa: BLE001 — figures are optional
                    ctx = None
            reason = ft.reason_for(cls, p, account)
            balances = ft.balance_lines(p, ctx) if cls == ft.INSUFFICIENT_BALANCE else ""
            action = ft.action_for(cls, p, account, ctx)
            from api.services.history import fmt_price as fmt_usdt
            attempts_n = p.failure_count if (getattr(event, "extra", None) or {}).get("stopped") else event.max_attempts
            tried = (f"We tried {attempts_n} times to buy {fmt_usdt(p.amount_usd)} USDT of {coin} "
                     f"on {event.exchange or 'your exchange'}.")
            if (getattr(event, "extra", None) or {}).get("stopped"):
                subject = f"{prefix}We've paused your {coin} purchase. No money was spent"
                tail = ("We've paused this purchase so it doesn't keep failing quietly — it won't run "
                        "again on its own. Once it's fixed, create the purchase again in AutoBuy "
                        "(it takes a minute) or reply to this email and we'll help.")
            else:
                subject = f"{prefix}Your {coin} buy didn't go through. No money was spent"
                nxt = (getattr(event, "extra", None) or {}).get("next_execution")
                tail = (f"Your next attempt is {fmt_run_time(nxt)}. If this is fixed by then, the buy runs normally."
                        if nxt is not None else "We'll try again at the next scheduled run.")
            text = (f"{tried} {reason} Nothing was bought and no money left your account. "
                    f"{balances}{action} {tail} {ft.HELP_FOOTER}")
        category = "failure" if event.type in ("buy_failed", "withdrawal_failed", "withdrawal_needs_info") else "confirmation"
        return await self.queue(user_email, subject, f"<p>{text}</p>", text, template="purchase",
                                category=category, user_id=getattr(event.user, "id", None))

    async def send_kyc_notification(self, user_email: str, kyc_status: str) -> bool:
        if kyc_status == "verified":
            subject = "✓ Identity verified"
            text = "Your account has been verified. Your scheduled purchases can now execute."
        elif kyc_status == "rejected":
            subject = "✗ Identity verification rejected"
            contact = f"Contact {settings.support_email}" if settings.support_email else "Contact support"
            text = f"Your identity verification was rejected. {contact} if you believe this is a mistake."
        else:
            return False  # no mail for intermediate states
        return await self._send(user_email, subject, f"<p>{text}</p>", text, template="kyc")


email_service = EmailService()
