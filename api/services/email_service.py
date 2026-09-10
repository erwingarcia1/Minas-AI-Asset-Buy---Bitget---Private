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

    async def send_purchase_notification(self, user_email: str, purchase, transaction) -> bool:
        crypto = transaction.crypto
        if transaction.status == "completed":
            subject = f"✓ Purchase complete: {transaction.amount_crypto:.8f} {crypto}"
            text = (f"You purchased {transaction.amount_crypto:.8f} {crypto} for "
                    f"{transaction.cost_usdt:.2f} USDT at {transaction.exchange_rate:.2f}. "
                    f"Order: {transaction.binance_order_id}")
        else:
            subject = f"✗ Purchase failed: {crypto}"
            text = (f"We tried to buy {crypto} for {purchase.amount_usdt:.2f} USDT but it failed: "
                    f"{transaction.error_message}. We'll retry automatically in about an hour.")
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
