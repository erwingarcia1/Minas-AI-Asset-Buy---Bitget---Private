"""Email through the platform's Resend outbox (v0.9.1).

Guardian spoke SMTP. In the platform every email leaves through one door:
api/services/email_outbox.py, a Redis queue the API's scheduler drains every
minute with retries and a dead-letter list. The worker only queues; it never
holds a provider credential and never opens a socket to a mail server, which
is what keeps `guardian/net.py` the only place that does.

`audience` picks the template, and the two are not interchangeable: the
operator format carries rule names, confidence and raw evidence, which is
exactly what must not reach a customer's mailbox. Default is operator so that
any caller which forgets to say gets the internal format sent to an internal
address, rather than the reverse.
"""
from __future__ import annotations

import html as html_mod

from guardian.alerts.base import AlertChannel, AlertMessage

TEMPLATE_BY_AUDIENCE = {"customer": "guardian_alert", "operator": "guardian_ops_alert"}


def email_configured() -> bool:
    """Whether the platform can send email at all (Resend key and sender)."""
    from api.config import settings
    key = (settings.resend_api_key or "").strip()
    return bool(key and settings.resend_from_email and not key.lower().startswith("your_"))


def _html(text: str) -> str:
    """The plain text as paragraphs, escaped. Plain text is the message
    (phishing-resistant by design, see alerts/base.py); the HTML part only
    exists so mail clients that prefer it show the same words."""
    return "".join(f"<p>{html_mod.escape(block).replace(chr(10), '<br/>')}</p>" for block in text.split("\n\n"))


class OutboxEmailChannel(AlertChannel):
    def __init__(self, recipients: list[str], *, audience: str = "operator"):
        if audience not in ("operator", "customer"):
            raise ValueError(f"unknown audience {audience!r}")
        self._recipients = [r for r in recipients if r]
        self._audience = audience

    def send(self, alert: AlertMessage) -> None:
        """Queues one message per recipient. Raises when the outbox is
        unavailable, so dispatch counts the channel as undelivered."""
        from api.services.email_outbox import enqueue_sync
        customer = self._audience == "customer"
        subject = alert.customer_subject() if customer else alert.operator_subject()
        text = alert.render_customer() if customer else alert.render()
        template = TEMPLATE_BY_AUDIENCE[self._audience]
        failed = [to for to in self._recipients
                  if not enqueue_sync(to, subject, _html(text), text, template=template, category="security")]
        if failed:
            raise RuntimeError(f"email outbox unavailable for {len(failed)} recipient(s)")
