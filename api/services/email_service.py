"""Transactional email via the Resend REST API.

Fire-and-forget by design: every public method logs failures and never raises,
so an email outage can never fail a purchase or a webhook. With no
RESEND_API_KEY configured (dev), sends are logged and skipped.
"""
import logging

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.email")

RESEND_URL = "https://api.resend.com/emails"


class EmailService:
    def __init__(self, api_key: str = "", from_email: str = ""):
        self.api_key = api_key or settings.resend_api_key
        self.from_email = from_email or settings.support_email

    async def _send(self, to: str, subject: str, html: str, text: str) -> bool:
        if not self.api_key or self.api_key.startswith("your_"):
            # unset or the .env.example placeholder: treat as unconfigured
            logger.info("email skipped (no RESEND_API_KEY): to=%s subject=%r", to, subject)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    RESEND_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"from": self.from_email, "to": [to],
                          "subject": subject, "html": html, "text": text},
                )
            if resp.status_code >= 400:
                logger.error("email send failed (%s): %s", resp.status_code, resp.text[:300])
                return False
            logger.info("email sent: to=%s subject=%r", to, subject)
            return True
        except Exception:
            logger.exception("email send raised: to=%s subject=%r", to, subject)
            return False

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
        html = f"<p>{text}</p>"
        return await self._send(user_email, subject, html, text)

    async def send_kyc_notification(self, user_email: str, kyc_status: str) -> bool:
        if kyc_status == "verified":
            subject = "✓ Identity verified"
            text = "Your account has been verified. Your scheduled purchases can now execute."
        elif kyc_status == "rejected":
            subject = "✗ Identity verification rejected"
            text = ("Your identity verification was rejected. "
                    f"Contact {self.from_email} if you believe this is a mistake.")
        else:
            return False  # no mail for intermediate states
        return await self._send(user_email, subject, f"<p>{text}</p>", text)


email_service = EmailService()
