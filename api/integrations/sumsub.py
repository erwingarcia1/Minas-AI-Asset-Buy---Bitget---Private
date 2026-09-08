"""Sumsub KYC integration: applicant creation, status lookup, webhook signatures.

A thin async façade over :class:`api.services.sumsub_client.SumsubClient`
(which owns request signing) exposing the plain functions the KYC routes
need. Credentials are read from the environment on every call
(``SUMSUB_APP_TOKEN``, ``SUMSUB_SECRET_KEY``, ``SUMSUB_WEBHOOK_SECRET``,
optional ``SUMSUB_API_URL`` / ``SUMSUB_LEVEL_NAME``) falling back to
``api.config.settings``, so rotated secrets take effect without a restart.

All network failures surface as :class:`SumsubError` after being logged;
callers decide whether that is a 502 or a degraded read.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Optional

import httpx

from api.config import settings
from api.services.sumsub_client import SumsubClient, SumsubError, map_review_to_kyc_status

logger = logging.getLogger("autobuy.integrations.sumsub")

__all__ = ["SumsubError", "create_applicant", "get_applicant_status",
           "verify_webhook_signature", "is_configured"]

# Digest algorithms Sumsub can be configured to sign webhooks with
# (header ``x-payload-digest-alg``). Default is HMAC_SHA256_HEX.
_DIGEST_ALGORITHMS = {
    "HMAC_SHA1_HEX": hashlib.sha1,
    "HMAC_SHA256_HEX": hashlib.sha256,
    "HMAC_SHA512_HEX": hashlib.sha512,
}

# Tests inject an httpx transport here; production leaves it None.
_transport: Optional[httpx.AsyncBaseTransport] = None


def _env(name: str, fallback: str) -> str:
    return os.environ.get(name) or fallback


def _credentials() -> tuple[str, str]:
    return (_env("SUMSUB_APP_TOKEN", settings.sumsub_app_token),
            _env("SUMSUB_SECRET_KEY", settings.sumsub_secret_key))


def is_configured() -> bool:
    """True when both the app token and secret key are present."""
    token, secret = _credentials()
    return bool(token and secret) and not token.startswith("your_")


def _client() -> SumsubClient:
    token, secret = _credentials()
    if not (token and secret):
        logger.error("sumsub credentials missing (SUMSUB_APP_TOKEN / SUMSUB_SECRET_KEY)")
        raise SumsubError("Sumsub is not configured")
    return SumsubClient(app_token=token, secret_key=secret,
                        base_url=_env("SUMSUB_API_URL", settings.sumsub_api_url),
                        transport=_transport)


async def create_applicant(email: str, first_name: str, last_name: str, *,
                           external_user_id: Optional[str] = None,
                           level_name: Optional[str] = None) -> str:
    """Create a Sumsub applicant and return its applicant id.

    ``external_user_id`` should be the platform's stable user id (it is what
    Sumsub echoes back in webhooks); it defaults to the email address.
    ``level_name`` selects the verification level (default ``SUMSUB_LEVEL_NAME``).

    Raises :class:`SumsubError` on missing credentials, transport failures,
    non-2xx responses, or a response without an applicant id.
    """
    email = (email or "").strip().lower()
    first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
    if not email or not first_name or not last_name:
        raise SumsubError("email, first_name and last_name are required")

    level = level_name or _env("SUMSUB_LEVEL_NAME", settings.sumsub_level_name)
    payload = {
        "externalUserId": external_user_id or email,
        "email": email,
        "fixedInfo": {"firstName": first_name, "lastName": last_name},
    }
    try:
        data = await _client().request("POST", f"/resources/applicants?levelName={level}", payload)
    except SumsubError as exc:
        logger.error("sumsub create_applicant failed for %s: %s", email, exc)
        raise
    applicant_id = data.get("id") if isinstance(data, dict) else None
    if not applicant_id:
        logger.error("sumsub create_applicant returned no id: %r", str(data)[:200])
        raise SumsubError("Sumsub response did not include an applicant id")
    logger.info("sumsub applicant created: %s (external=%s)", applicant_id, payload["externalUserId"])
    return str(applicant_id)


async def get_applicant_status(applicant_id: str) -> Dict[str, Any]:
    """Fetch an applicant's review state.

    Returns ``{"applicant_id", "review_status", "review_answer",
    "reject_labels", "kyc_status", "raw"}`` where ``kyc_status`` is the
    platform mapping (pending / verified / rejected) and ``raw`` is Sumsub's
    unmodified response body.
    """
    if not applicant_id:
        raise SumsubError("applicant_id is required")
    try:
        data = await _client().request("GET", f"/resources/applicants/{applicant_id}/status")
    except SumsubError as exc:
        logger.warning("sumsub get_applicant_status failed for %s: %s", applicant_id, exc)
        raise
    review_result = data.get("reviewResult") or {}
    review_status = data.get("reviewStatus", "") or ""
    review_answer = review_result.get("reviewAnswer")
    status = {
        "applicant_id": applicant_id,
        "review_status": review_status,
        "review_answer": review_answer,
        "reject_labels": list(review_result.get("rejectLabels") or []),
        "kyc_status": map_review_to_kyc_status(review_status, review_answer),
        "raw": data,
    }
    logger.info("sumsub status for %s: %s/%s -> %s", applicant_id, review_status,
                review_answer, status["kyc_status"])
    return status


def verify_webhook_signature(signature: str, body: bytes | str, secret: Optional[str] = None,
                             algorithm: str = "HMAC_SHA256_HEX") -> bool:
    """Check a Sumsub webhook's ``x-payload-digest`` header against the raw body.

    ``signature`` is the hex digest Sumsub sent, ``body`` the exact request
    bytes, ``secret`` the webhook secret (default ``SUMSUB_WEBHOOK_SECRET``),
    ``algorithm`` the value of ``x-payload-digest-alg``. Returns False, never
    raises, when anything is missing or malformed.
    """
    secret = secret or _env("SUMSUB_WEBHOOK_SECRET", settings.sumsub_webhook_secret)
    if not secret:
        logger.warning("sumsub webhook rejected: SUMSUB_WEBHOOK_SECRET not configured")
        return False
    if not signature:
        logger.warning("sumsub webhook rejected: missing x-payload-digest")
        return False
    digest = _DIGEST_ALGORITHMS.get((algorithm or "").upper())
    if digest is None:
        logger.warning("sumsub webhook rejected: unsupported digest algorithm %r", algorithm)
        return False
    if isinstance(body, str):
        body = body.encode()
    expected = hmac.new(secret.encode(), body, digest).hexdigest()
    ok = hmac.compare_digest(expected, signature.strip().lower())
    if not ok:
        logger.warning("sumsub webhook rejected: signature mismatch")
    return ok
