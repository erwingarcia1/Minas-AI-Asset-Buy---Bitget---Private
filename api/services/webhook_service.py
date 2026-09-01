"""Outbound user webhooks: HMAC-signed JSON POSTs with capped retries.

Payloads are signed with the endpoint's secret (X-AutoBuy-Signature:
hex HMAC-SHA256 of the raw body). Delivery is fire-and-forget from the
purchase flow; per-endpoint status is stored for the user to inspect."""
import asyncio
import hashlib
import hmac
import json
import logging

import httpx
from sqlalchemy import select

from api.models import WebhookEndpoint

logger = logging.getLogger("autobuy.webhooks_out")

RETRY_DELAYS_S = (1, 5, 25)


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver_one(endpoint: WebhookEndpoint, payload: dict) -> str:
    """POST with retries; returns the status string stored on the endpoint."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json",
               "X-AutoBuy-Signature": sign(endpoint.secret, body),
               "X-AutoBuy-Event": str(payload.get("event", ""))}
    last = "failed: not attempted"
    for i, delay in enumerate((0,) + RETRY_DELAYS_S):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(endpoint.url, content=body, headers=headers)
            if resp.status_code < 300:
                return str(resp.status_code)
            last = f"failed: HTTP {resp.status_code}"
            if 400 <= resp.status_code < 500:
                break  # the receiver rejected it; retrying won't help
        except httpx.HTTPError as exc:
            last = f"failed: {type(exc).__name__}"
    return last


async def dispatch(db, user_id, event: str, data: dict) -> int:
    """Deliver an event to every enabled endpoint of the user. Returns the
    number of endpoints attempted. Never raises."""
    try:
        endpoints = (await db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.user_id == user_id,
                WebhookEndpoint.enabled.is_(True)))).scalars().all()
        for ep in endpoints:
            status = await deliver_one(ep, {"event": event, "data": data})
            ep.last_status = status
            if status.startswith("failed"):
                logger.warning("webhook %s delivery failed: %s", ep.id, status)
        if endpoints:
            await db.commit()
        return len(endpoints)
    except Exception:
        logger.exception("webhook dispatch failed for user %s", user_id)
        return 0
