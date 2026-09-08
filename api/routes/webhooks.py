"""Inbound webhook endpoints.

- ``/sumsub``: applicant lifecycle events (HMAC-verified via x-payload-digest)
- ``/resend``: delivery events — a stub for now; full processing (suppression
  list, alerting) is Phase 3 work.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.integrations.sumsub import verify_webhook_signature
from api.routes.kyc import apply_review_event

logger = logging.getLogger("autobuy.webhooks")

router = APIRouter()


@router.post("/sumsub")
async def sumsub_events(request: Request, db: AsyncSession = Depends(get_db)):
    """Sumsub applicant webhook (applicantReviewed, applicantPending, ...).

    The raw body is verified against ``x-payload-digest`` using the algorithm
    in ``x-payload-digest-alg`` (default HMAC_SHA256_HEX) before anything is
    parsed; every event is logged; review results update the user's
    kyc_status through the same path as ``POST /api/kyc/webhook``."""
    raw = await request.body()
    if not verify_webhook_signature(request.headers.get("x-payload-digest", ""), raw,
                                    algorithm=request.headers.get("x-payload-digest-alg", "HMAC_SHA256_HEX")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="Body is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")

    logger.info("sumsub webhook: type=%s applicant=%s reviewStatus=%s answer=%s",
                payload.get("type"), payload.get("applicantId"), payload.get("reviewStatus"),
                (payload.get("reviewResult") or {}).get("reviewAnswer"))
    result = await apply_review_event(db, payload)
    return {"received": True, "type": payload.get("type"), **result}


@router.post("/resend", status_code=202)
async def resend_events(request: Request):
    """Accept Resend delivery events (bounce/complaint). Stub: log only."""
    payload = await request.json()
    logger.info("resend webhook: type=%s", payload.get("type"))
    return {"received": True}
