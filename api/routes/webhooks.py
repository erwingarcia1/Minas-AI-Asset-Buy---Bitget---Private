"""Inbound webhook endpoints.

Resend bounce/complaint handling is a stub for now — full processing
(suppression list, alerting) is Phase 3 work.
"""
import logging

from fastapi import APIRouter, Request

logger = logging.getLogger("autobuy.webhooks")

router = APIRouter()


@router.post("/resend", status_code=202)
async def resend_events(request: Request):
    """Accept Resend delivery events (bounce/complaint). Stub: log only."""
    payload = await request.json()
    logger.info("resend webhook: type=%s", payload.get("type"))
    return {"received": True}
