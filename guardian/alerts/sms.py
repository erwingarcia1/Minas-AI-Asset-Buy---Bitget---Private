from __future__ import annotations

from guardian import net
from guardian.alerts.base import AlertChannel, AlertMessage, portal_url

# Two SMS segments. Shared by every SMS transport: SMS is a pager, not a
# report - the full evidence goes out on Telegram/email in parallel.
SMS_MAX_LEN = 320


def format_sms_body(alert: AlertMessage, max_len: int = SMS_MAX_LEN) -> str:
    """Short, action-focused SMS body: product name, severity, alert type,
    affected account, what happened, and a log-in instruction. The login
    link (the platform's /guardian pages, from APP_BASE_URL) is appended
    last and is never truncated away - only the free-text detail shrinks."""
    link = portal_url()
    action = (f" Log in now to review: {link}" if link
              else " Log in to Minas AI Guardian now to review.")
    prefix = f"Minas AI Guardian {alert.severity} alert: {alert.rule} on {alert.account_label}."
    detail = f" {alert.message}." if alert.message else ""
    room = max_len - len(prefix) - len(action)
    if len(detail) > room:
        detail = detail[: max(0, room - 3)] + "..." if room > 3 else ""
    return prefix + detail + action


def build_sms_channel(recipients: list[str]) -> AlertChannel | None:
    """Return an SMS channel for `recipients` based on configured env, or None
    if no SMS provider is set. ClickSend is preferred when its credentials are
    present; Twilio is the fallback. Callers no-op cleanly when this is None."""
    recipients = [r for r in recipients if r]
    if not recipients:
        return None
    from api.config import settings
    cs_user, cs_key = settings.clicksend_username, settings.clicksend_api_key
    if cs_user and cs_key:
        from guardian.alerts.clicksend import ClickSendSmsChannel

        return ClickSendSmsChannel(cs_user, cs_key, settings.clicksend_from or None, recipients)
    sid, token, frm = settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number
    if sid and token and frm:
        return TwilioSmsChannel(sid, token, frm, recipients)
    return None


class TwilioSmsChannel(AlertChannel):
    """SMS via Twilio's REST API. SMS is CRITICAL-only in the default
    routing; the body is truncated hard because SMS is a pager, not a
    report - the full evidence goes out on Telegram/email in parallel."""

    MAX_LEN = SMS_MAX_LEN

    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_numbers: list[str], timeout: float = 15.0):
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._to = to_numbers
        self._timeout = timeout

    def send(self, alert: AlertMessage) -> list[dict]:
        """Sends to every recipient; returns one delivery receipt per number:
        {to, provider, provider_message_id, status}."""
        body = format_sms_body(alert, self.MAX_LEN)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json"
        receipts = []
        for number in self._to:
            resp = net.post(
                url,
                auth=(self._sid, self._token),
                data={"From": self._from, "To": number, "Body": body},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            try:
                sid = resp.json().get("sid")
            except ValueError:
                sid = None
            receipts.append({"to": number, "provider": "twilio", "provider_message_id": sid, "status": "sent"})
        return receipts
