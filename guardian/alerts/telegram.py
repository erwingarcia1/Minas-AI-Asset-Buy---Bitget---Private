"""Telegram through the platform's sender (v0.9.1).

api/services/telegram.py owns the bot token, the timeout and retry, the
per-minute rate limit and the log redaction of the token. The worker is its
own process, so the in-process limiter there is correct for it. Guardian's
dedup and cooldowns already decide what fires, so no burst collapsing is
asked for here (a collapsed message would count as undelivered).

Emails are masked before anything leaves (CLAUDE.md: customer emails are
masked in anything that leaves the system); addresses and amounts stay, the
operator needs them.
"""
from __future__ import annotations

from guardian import net
from guardian.alerts.base import AlertChannel, AlertMessage


class TelegramChannel(AlertChannel):
    def __init__(self, chat_id: str | None = None):
        # None = the platform's TELEGRAM_CHAT_ID; a value = another chat of the same bot.
        self._chat_id = chat_id

    def send(self, alert: AlertMessage) -> None:
        from api.services import telegram
        net.refuse_if_under_test("a Telegram message", "api.telegram.org")
        text = telegram._EMAIL_RE.sub(lambda m: telegram.mask_email(m.group(0)), alert.render())
        result = telegram.send_sync(text, None, chat_id=self._chat_id)
        if not result.ok:
            raise RuntimeError(f"telegram send failed: {result.error or 'suppressed'}")
