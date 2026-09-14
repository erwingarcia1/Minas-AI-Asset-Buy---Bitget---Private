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

    async def send_purchase_event_email(self, user_email: str, event) -> bool:
        """One email per Auto-Buy attempt outcome (see purchase_events)."""
        p, tx = event.purchase, event.tx
        coin = p.crypto_symbol
        amount = tx.amount_executed or 0.0
        cost = tx.cost_usd if tx.cost_usd is not None else p.amount_usd
        price = f" at {tx.fill_price:.2f}" if tx.fill_price else ""
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
        else:  # buy_failed
            subject = f"{prefix}✗ Purchase failed: {coin}"
            text = (f"We tried to buy {coin} for {p.amount_usd:.2f} USD on {event.exchange or 'your exchange'} "
                    f"but it failed: {event.error}. Attempt {event.attempt}/{event.max_attempts}"
                    + ("; we will retry on the next run." if event.attempt < event.max_attempts
                       else "; the purchase is now marked failed."))
        return await self._send(user_email, subject, f"<p>{text}</p>", text, template="purchase")

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
