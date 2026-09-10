"""KYC verification flow (Sumsub): initiate, status sync, review webhook.

kyc_status is owned by this flow — it is deliberately NOT user-writable
anywhere else (self-serve verification was a compliance hole).
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.integrations import sumsub
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
    kyc_status: str                         # pending | verified | rejected (platform status)
    applicant_id: Optional[str] = None
    review_status: Optional[str] = None     # Sumsub: init (nothing submitted) | pending (under review) | completed
    reject_labels: List[str] = []
    rejection_reason: Optional[str] = None  # Sumsub moderation comment, when synced
    retryable: bool = True                  # False when Sumsub marked the rejection FINAL
    synced: bool = False                    # True when this response came from Sumsub, not the stored status


class CreateApplicantRequest(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)


class CreateApplicantResponse(BaseModel):
    applicant_id: str
    kyc_status: str
    created: bool  # False when the account already had an applicant


class ApplicantStatusResponse(BaseModel):
    applicant_id: str
    kyc_status: str
    review_status: str
    review_answer: Optional[str] = None
    reject_labels: List[str] = []


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
    sync: bool = Query(False, description="Force a Sumsub lookup even when the stored status is final"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's KYC status, synced from Sumsub while it is still pending.

    Works on localhost without the webhook: while the stored status is
    `pending` and an applicant exists, Sumsub is asked for the applicant's
    review state and `users.kyc_status` is updated from it. A final stored
    status (`verified` / `rejected`) is returned as-is without calling Sumsub
    unless `?sync=1` is passed (the page does that after SDK events and on
    "Refresh", so a rejected-then-resubmitted applicant can move back to
    pending)."""
    stored = StatusResponse(kyc_status=current_user.kyc_status, applicant_id=current_user.kyc_sumsub_id)
    if not current_user.kyc_sumsub_id:
        return stored
    if current_user.kyc_status in ("verified", "rejected") and not sync:
        return stored
    if not sumsub.is_configured():
        logger.warning("kyc status not synced for %s: Sumsub is not configured", current_user.id)
        return stored
    try:
        info = await sumsub.get_applicant_status(current_user.kyc_sumsub_id)
    except SumsubError as exc:
        logger.warning("kyc status fetch failed for %s: %s", current_user.id, exc)
        return stored  # stale, not an error
    status = info["kyc_status"]
    if status != current_user.kyc_status:
        current_user.kyc_status = status
        await db.commit()
        logger.info("kyc status for user %s -> %s (status sync)", current_user.id, status)
    review_result = (info.get("raw") or {}).get("reviewResult") or {}
    return StatusResponse(
        kyc_status=status, applicant_id=current_user.kyc_sumsub_id, review_status=info.get("review_status") or None,
        reject_labels=list(info.get("reject_labels") or []),
        rejection_reason=review_result.get("moderationComment") or review_result.get("clientComment") or None,
        retryable=review_result.get("reviewRejectType") != "FINAL", synced=True)


async def apply_review_event(db: AsyncSession, payload: dict) -> dict:
    """Apply a Sumsub review payload to the matching user's kyc_status.

    Returns ``{"applicant_id", "known", "kyc_status", "changed"}``. Shared by
    ``POST /api/kyc/webhook`` and ``POST /api/webhooks/sumsub``; the caller is
    responsible for signature verification.
    """
    applicant_id = payload.get("applicantId")
    if not applicant_id:
        raise HTTPException(status_code=422, detail="Missing applicantId")

    user = (await db.execute(
        select(User).where(User.kyc_sumsub_id == applicant_id))).scalar_one_or_none()
    if user is None:
        logger.warning("webhook for unknown applicant %s", applicant_id)
        return {"applicant_id": applicant_id, "known": False, "kyc_status": None, "changed": False}

    status = map_review_to_kyc_status(
        payload.get("reviewStatus", ""),
        (payload.get("reviewResult") or {}).get("reviewAnswer"))
    changed = status != user.kyc_status
    if changed:
        user.kyc_status = status
        await db.commit()
        logger.info("kyc status for user %s -> %s", user.id, status)
        await email_service.send_kyc_notification(user.email, status)
    return {"applicant_id": applicant_id, "known": True, "kyc_status": status, "changed": changed}


@router.post("/webhook")
async def sumsub_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Sumsub review-result callback. HMAC-verified; updates kyc_status and
    notifies the user by email."""
    raw = await request.body()
    if not verify_webhook_signature(raw, request.headers.get("x-payload-digest", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    await apply_review_event(db, await request.json())
    return {"received": True}


@router.post("/create-applicant", response_model=CreateApplicantResponse)
async def create_applicant(
    payload: CreateApplicantRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create the caller's Sumsub applicant (legal name + account email) and
    remember its id. Idempotent: an account that already has an applicant gets
    the existing id back. The email must match the account — KYC identity is
    bound to the login, never to an arbitrary address."""
    if payload.email.lower() != current_user.email.lower():
        raise HTTPException(status_code=403, detail="Applicant email must match your account email")
    if current_user.kyc_status == "verified":
        raise HTTPException(status_code=400, detail="Already verified")
    if current_user.kyc_sumsub_id:
        return CreateApplicantResponse(applicant_id=current_user.kyc_sumsub_id,
                                       kyc_status=current_user.kyc_status, created=False)
    if not sumsub.is_configured():
        raise HTTPException(status_code=503, detail="KYC provider is not configured")
    try:
        applicant_id = await sumsub.create_applicant(
            current_user.email, payload.first_name, payload.last_name,
            external_user_id=str(current_user.id))
    except SumsubError as exc:
        logger.error("create-applicant failed for %s: %s", current_user.id, exc)
        raise HTTPException(status_code=502, detail="KYC provider error")
    current_user.kyc_sumsub_id = applicant_id
    await db.commit()
    return CreateApplicantResponse(applicant_id=applicant_id,
                                   kyc_status=current_user.kyc_status, created=True)


@router.get("/applicant/{applicant_id}/status", response_model=ApplicantStatusResponse)
async def applicant_status(
    applicant_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Live review state for an applicant. Users may only query their own
    applicant (admins: any); the caller's kyc_status is synced on the way."""
    own = applicant_id == current_user.kyc_sumsub_id
    if not own and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not your applicant")
    if not sumsub.is_configured():
        raise HTTPException(status_code=503, detail="KYC provider is not configured")
    try:
        status = await sumsub.get_applicant_status(applicant_id)
    except SumsubError as exc:
        logger.warning("applicant status fetch failed for %s: %s", applicant_id, exc)
        raise HTTPException(status_code=502, detail="KYC provider error")
    if own and status["kyc_status"] != current_user.kyc_status:
        current_user.kyc_status = status["kyc_status"]
        await db.commit()
        logger.info("kyc status for user %s -> %s (status poll)", current_user.id, status["kyc_status"])
    return ApplicantStatusResponse(
        applicant_id=applicant_id, kyc_status=status["kyc_status"],
        review_status=status["review_status"], review_answer=status["review_answer"],
        reject_labels=status["reject_labels"])
