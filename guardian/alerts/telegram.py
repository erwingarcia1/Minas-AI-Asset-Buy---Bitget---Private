"""Telegram through the platform's sender (v0.9.1).

api/services/telegram.py owns the bot token, the timeout and retry, the
per-minute rate limit and the log redaction of the token. The worker is its
own process, so the in-process limiter there is correct for it. Guardian's
dedup and cooldowns already decide what fires, so no burst collapsing is
asked for here (a collapsed message would count as undelivered).

The platform sender's no-secrets contract holds for these messages too: they
land in the same chats. Emails are masked (two characters and the domain) and
wallet addresses show their first 6 and last 4 characters, through the same
helpers the platform uses. Amounts, order references and rule names stay -
the operator needs them, and a reference is not an address.
"""
from __future__ import annotations

import re

from guardian import net
from guardian.alerts.base import AlertChannel, AlertMessage

# What a wallet address, a transaction hash or a key id looks like inside our
# own text: a run of 25 or more letters and digits with no separator, and at
# least one digit. Our copy never produces one. Rule names carry underscores,
# which is why this is narrower than the platform's `_LONG_TOKEN_RE` (that one
# scrubs error strings, where a rule name never appears). The 25 is the
# shortest common address (XRP, legacy Bitcoin).
_ADDRESS_RUN_RE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z]*\d)[A-Za-z0-9]{25,}(?![A-Za-z0-9])")


def _strings(value) -> set[str]:
    if isinstance(value, str):
        return {value} if value.strip() else set()
    if isinstance(value, dict):
        return set().union(*(_strings(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple, set)):
        return set().union(*(_strings(v) for v in value)) if value else set()
    return set()


def _address_values(node) -> set[str]:
    """Every string under an evidence key that names an address (`address`,
    `addresses`, `to_address`, ...), at any depth: the detectors put the
    address in the evidence and the same address in the free-text message,
    and `_report_unidentifiable` nests one inside an `example` record."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if "address" in str(key).lower():
                found |= _strings(value)
            else:
                found |= _address_values(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            found |= _address_values(item)
    return found


def redact(text: str, evidence: dict | None = None) -> str:
    """The text as it may leave for Telegram: emails and addresses masked the
    way api/services/telegram.py masks them.

    Addresses are found two ways. By value, from the evidence fields that
    carry them, which also catches the same address inside the message line
    and catches short or unusual formats. By shape, for text that comes with
    no evidence at all - the daily digest is one body built from stored
    messages."""
    from api.services import telegram

    text = telegram._EMAIL_RE.sub(lambda m: telegram.mask_email(m.group(0)), text)
    for address in sorted(_address_values(evidence or {}), key=len, reverse=True):
        text = text.replace(address, telegram.mask_address(address))
    return _ADDRESS_RUN_RE.sub(lambda m: telegram.mask_address(m.group(0)), text)


class TelegramChannel(AlertChannel):
    def __init__(self, chat_id: str | None = None):
        # None = the platform's TELEGRAM_CHAT_ID; a value = another chat of the same bot.
        self._chat_id = chat_id

    def send(self, alert: AlertMessage) -> None:
        from api.services import telegram
        net.refuse_if_under_test("a Telegram message", "api.telegram.org")
        result = telegram.send_sync(redact(alert.render(), alert.evidence), None, chat_id=self._chat_id)
        if not result.ok:
            raise RuntimeError(f"telegram send failed: {result.error or 'suppressed'}")
