"""Operational Slack alerts via an incoming-webhook URL (no-op when unset)."""
import logging

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.slack")


async def send_slack(text: str) -> bool:
    url = settings.slack_webhook_url
    if not url or url.startswith("your_"):
        logger.info("slack skipped (SLACK_WEBHOOK_URL unset): %r", text[:80])
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"text": text})
        return resp.status_code < 300
    except Exception:
        logger.exception("slack send raised")
        return False
