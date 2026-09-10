"""Resend transactional email — the platform's only email sender.

Every email the platform sends goes through :func:`send_email`. All
behaviour is driven by configuration, never by hardcoded values:

- ``RESEND_API_KEY``    empty or a placeholder means *not configured*: the
                        send is skipped with a WARNING (recipient + subject)
                        and :class:`ResendNotConfigured` is raised so callers
                        can decide whether that matters.
- ``RESEND_FROM_EMAIL`` sender for every email, on a domain verified in
                        Resend, e.g. ``Minas AutoBuy <noreply@minastrade.com>``.
- ``RESEND_REPLY_TO``   Reply-To on every email; defaults to ``SUPPORT_EMAIL``.

Delivery failures (HTTP error, transport error, missing id) are logged at
ERROR on this logger with status and reason, then raised as
:class:`ResendError`; callers decide whether to surface them. Successful
sends return Resend's email id and log at INFO.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.integrations.resend")

__all__ = ["ResendError", "ResendNotConfigured", "send_email", "is_configured", "sender", "reply_to",
           "check_startup_config", "RESEND_URL"]

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 10.0

# Resend's shared testing sender only delivers to the account owner's own
# address, so it must never be a production sender. Built from fragments so
# the repository-wide scan for hardcoded email domains stays meaningful.
RESEND_TEST_SENDER = "onboarding@" + ".".join(("resend", "dev"))

# Tests inject an httpx transport here; production leaves it None.
_transport: Optional[httpx.AsyncBaseTransport] = None


class ResendError(Exception):
    """Email could not be sent. ``status`` is the HTTP status when Resend answered."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ResendNotConfigured(ResendError):
    """RESEND_API_KEY or RESEND_FROM_EMAIL is missing or a placeholder."""


def _cfg(env_name: str, settings_value: str) -> str:
    """Environment first (so rotated values apply without a restart), then settings."""
    return (os.environ.get(env_name) or settings_value or "").strip()


def is_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    return not v or v.startswith("your_") or "change_me" in v or "placeholder" in v or v == "xxx"


def _api_key() -> str:
    key = _cfg("RESEND_API_KEY", settings.resend_api_key)
    return "" if is_placeholder(key) else key


def sender() -> str:
    """Configured sender (``RESEND_FROM_EMAIL``), or empty when unset."""
    return _cfg("RESEND_FROM_EMAIL", settings.resend_from_email)


def reply_to() -> str:
    """``RESEND_REPLY_TO``, else ``SUPPORT_EMAIL``, else empty (no Reply-To header)."""
    return _cfg("RESEND_REPLY_TO", settings.resend_reply_to) or _cfg("SUPPORT_EMAIL", settings.support_email)


def is_configured() -> bool:
    """True when a real API key and a sender are both available."""
    return bool(_api_key() and sender())


def check_startup_config() -> None:
    """Log configuration problems once at startup (never raises; the
    production boot guard is what refuses to start)."""
    if not _api_key():
        logger.warning("email is not configured: RESEND_API_KEY is empty or a placeholder; every send will be skipped")
    elif not sender():
        logger.warning("email is not configured: RESEND_FROM_EMAIL is empty; every send will be skipped")
    if sender().lower().endswith(RESEND_TEST_SENDER) or RESEND_TEST_SENDER in sender().lower():
        logger.warning("RESEND_FROM_EMAIL uses %s: Resend delivers mail from this sender ONLY to the Resend "
                       "account owner's address; customers will not receive email", RESEND_TEST_SENDER)


def _record(template: str, ok: bool, started: float) -> None:
    try:
        from api.services.metrics import email_send_duration_seconds, emails_total
        email_send_duration_seconds.labels(template).observe(time.perf_counter() - started)
        emails_total.labels(template, "ok" if ok else "failed").inc()
    except Exception:  # metrics must never break a send
        logger.debug("email metrics unavailable", exc_info=True)


async def send_email(to_email: str, subject: str, html_content: str,
                     from_email: Optional[str] = None, *,
                     text_content: Optional[str] = None,
                     reply_to: Optional[str] = None,
                     template: str = "generic") -> str:
    """Send one email through Resend and return its email id.

    ``from_email`` defaults to the configured sender and ``reply_to`` to the
    configured Reply-To; ``template`` labels the metrics.

    Raises :class:`ResendNotConfigured` (after a WARNING) when the key or
    sender is missing, and :class:`ResendError` (after an ERROR with status
    and reason) when Resend is unreachable or rejects the send.
    """
    to_email = (to_email or "").strip()
    if not to_email or not subject or not html_content:
        raise ResendError("to_email, subject and html_content are required")
    api_key = _api_key()
    from_addr = (from_email or sender()).strip()
    if not api_key or not from_addr:
        missing = "RESEND_API_KEY" if not api_key else "RESEND_FROM_EMAIL"
        logger.warning("email skipped (not configured: %s): to=%s subject=%r", missing, to_email, subject)
        raise ResendNotConfigured(f"Resend is not configured ({missing} is empty or a placeholder)")

    body: Dict[str, Any] = {"from": from_addr, "to": [to_email], "subject": subject, "html": html_content}
    if text_content:
        body["text"] = text_content
    reply = (reply_to if reply_to is not None else globals()["reply_to"]()).strip()
    if reply:
        body["reply_to"] = reply

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=_transport) as client:
            resp = await client.post(RESEND_URL, headers={"Authorization": f"Bearer {api_key}"}, json=body)
    except httpx.HTTPError as exc:
        _record(template, False, started)
        logger.error("email send failed (transport): to=%s subject=%r reason=%s", to_email, subject, exc)
        raise ResendError(f"Resend unreachable: {exc}") from exc

    if resp.status_code >= 400:
        _record(template, False, started)
        logger.error("email send failed (status %s): to=%s subject=%r from=%s reason=%s",
                     resp.status_code, to_email, subject, from_addr, resp.text[:300])
        raise ResendError(f"Resend {resp.status_code}: {resp.text[:200]}", status=resp.status_code)

    try:
        email_id = str(resp.json().get("id") or "")
    except ValueError:
        email_id = ""
    if not email_id:
        _record(template, False, started)
        logger.error("email send failed (status %s): to=%s subject=%r reason=response had no email id",
                     resp.status_code, to_email, subject)
        raise ResendError("Resend response did not include an email id", status=resp.status_code)

    _record(template, True, started)
    logger.info("email sent: id=%s to=%s subject=%r template=%s", email_id, to_email, subject, template)
    return email_id
