"""KYC verification flow (Sumsub): initiate, status sync, review webhook.

kyc_status is owned by this flow — it is deliberately NOT user-writable
anywhere else (self-serve verification was a compliance hole).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import User
from api.routes.user import get_current_user
from api.services.email_service import email_service
from api.services.sumsub_client import (
    SumsubClient, SumsubError, map_review_to_kyc_status, verify_webhook_signature)

logger = logging.getLogger("autobuy.kyc")

router = APIRouter()


class InitiateResponse(BaseModel):
    access_token: str
    applicant_id: str


class StatusResponse(BaseModel):
    kyc_status: str


def _client() -> SumsubClient:
    client = SumsubClient()
    if not client.configured:
        raise HTTPException(status_code=503, detail="KYC provider is not configured")
    return client


@router.post("/initiate", response_model=InitiateResponse)
async def initiate_kyc(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create (or reuse) the Sumsub applicant and return a WebSDK token."""
    if current_user.kyc_status == "verified":
        raise HTTPException(status_code=400, detail="Already verified")
    client = _client()
    try:
        if not current_user.kyc_sumsub_id:
            current_user.kyc_sumsub_id = await client.create_applicant(
                str(current_user.id), current_user.email)
            await db.commit()
        token = await client.get_access_token(str(current_user.id))
    except SumsubError as exc:
        logger.error("kyc initiate failed for %s: %s", current_user.id, exc)
        raise HTTPException(status_code=502, detail="KYC provider error")
    return InitiateResponse(access_token=token, applicant_id=current_user.kyc_sumsub_id)


@router.get("/status", response_model=StatusResponse)
async def kyc_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sync and return the caller's KYC status from Sumsub."""
    if not current_user.kyc_sumsub_id:
        return StatusResponse(kyc_status=current_user.kyc_status)
    client = _client()
    try:
        status = await client.get_applicant_status(current_user.kyc_sumsub_id)
    except SumsubError as exc:
        logger.warning("kyc status fetch failed for %s: %s", current_user.id, exc)
        return StatusResponse(kyc_status=current_user.kyc_status)  # stale, not an error
    if status != current_user.kyc_status:
        current_user.kyc_status = status
        await db.commit()
    return StatusResponse(kyc_status=status)


@router.post("/webhook")
async def sumsub_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Sumsub review-result callback. HMAC-verified; updates kyc_status and
    notifies the user by email."""
    raw = await request.body()
    if not verify_webhook_signature(raw, request.headers.get("x-payload-digest", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    applicant_id = payload.get("applicantId")
    if not applicant_id:
        raise HTTPException(status_code=422, detail="Missing applicantId")

    user = (await db.execute(
        select(User).where(User.kyc_sumsub_id == applicant_id))).scalar_one_or_none()
    if user is None:
        logger.warning("webhook for unknown applicant %s", applicant_id)
        return {"received": True}

    status = map_review_to_kyc_status(
        payload.get("reviewStatus", ""),
        (payload.get("reviewResult") or {}).get("reviewAnswer"))
    if status != user.kyc_status:
        user.kyc_status = status
        await db.commit()
        logger.info("kyc status for user %s -> %s", user.id, status)
        await email_service.send_kyc_notification(user.email, status)
    return {"received": True}
