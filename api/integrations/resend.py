"""Resend transactional email integration.

Exposes :func:`send_email`, which returns Resend's email id on success and
raises :class:`ResendError` (after logging) on any failure, so callers can
report delivery honestly. ``RESEND_API_KEY`` is read from the environment on
each call (falling back to ``api.config.settings``); the ``.env.example``
placeholder counts as unconfigured. ``RESEND_FROM_EMAIL`` overrides the
default sender.

This complements :mod:`api.services.email_service`, which is deliberately
fire-and-forget for purchase/KYC notifications that must never fail a trade.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.integrations.resend")

__all__ = ["ResendError", "send_email", "is_configured", "RESEND_URL", "DEFAULT_FROM_EMAIL"]

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM_EMAIL = "noreply@autobuy.platform"
TIMEOUT_SECONDS = 10.0

# Tests inject an httpx transport here; production leaves it None.
_transport: Optional[httpx.AsyncBaseTransport] = None


class ResendError(Exception):
    """Email could not be sent. ``status`` is the HTTP status when Resend answered."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _api_key() -> str:
    key = os.environ.get("RESEND_API_KEY") or settings.resend_api_key or ""
    return "" if key.startswith("your_") else key


def is_configured() -> bool:
    """True when a real ``RESEND_API_KEY`` is available."""
    return bool(_api_key())


def default_from_email() -> str:
    """Sender address: ``RESEND_FROM_EMAIL`` or the platform default."""
    return os.environ.get("RESEND_FROM_EMAIL") or DEFAULT_FROM_EMAIL


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

    ``from_email`` defaults to :func:`default_from_email`; ``text_content`` is
    an optional plain-text alternative; ``template`` labels the metrics.

    Raises :class:`ResendError` when the API key is missing, Resend is
    unreachable, or it answers with a non-2xx status.
    """
    to_email = (to_email or "").strip()
    if not to_email or not subject or not html_content:
        raise ResendError("to_email, subject and html_content are required")
    api_key = _api_key()
    if not api_key:
        logger.warning("email not sent (RESEND_API_KEY unset): to=%s subject=%r", to_email, subject)
        raise ResendError("Resend is not configured")

    body: Dict[str, Any] = {"from": from_email or default_from_email(), "to": [to_email],
                            "subject": subject, "html": html_content}
    if text_content:
        body["text"] = text_content
    if reply_to:
        body["reply_to"] = reply_to

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=_transport) as client:
            resp = await client.post(RESEND_URL, headers={"Authorization": f"Bearer {api_key}"}, json=body)
    except httpx.HTTPError as exc:
        _record(template, False, started)
        logger.error("email send failed (transport): to=%s subject=%r err=%s", to_email, subject, exc)
        raise ResendError(f"Resend unreachable: {exc}") from exc

    if resp.status_code >= 400:
        _record(template, False, started)
        logger.error("email send failed (%s): to=%s subject=%r body=%s",
                     resp.status_code, to_email, subject, resp.text[:300])
        raise ResendError(f"Resend {resp.status_code}: {resp.text[:200]}", status=resp.status_code)

    try:
        email_id = str(resp.json().get("id") or "")
    except ValueError:
        email_id = ""
    if not email_id:
        _record(template, False, started)
        logger.error("email send returned no id: to=%s subject=%r", to_email, subject)
        raise ResendError("Resend response did not include an email id", status=resp.status_code)

    _record(template, True, started)
    logger.info("email sent: id=%s to=%s subject=%r", email_id, to_email, subject)
    return email_id
