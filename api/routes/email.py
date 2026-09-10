"""Outbound email routes on top of :mod:`api.integrations.resend`.

Both routes are authenticated and, to keep the platform from becoming an
open relay, only ever send to the caller's own address (admins may notify
any address). Delivery failures are reported honestly: 503 when Resend is
not configured, 502 when it rejected the send.
"""
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.integrations import resend
from api.integrations.resend import ResendError
from api.models import User
from api.routes.user import get_current_user
from api.services.rate_limit import limiter, user_or_ip_key
from api.utils.links import build_app_link

logger = logging.getLogger("autobuy.email.routes")

router = APIRouter()

VERIFY_TOKEN_TTL = timedelta(hours=24)


class SendVerificationRequest(BaseModel):
    email: EmailStr


class SendNotificationRequest(BaseModel):
    email: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=5000)


class EmailSentResponse(BaseModel):
    sent: bool
    email_id: Optional[str] = None


def make_verification_token(user_id: str) -> str:
    """Short-lived JWT (type=email_verify) that identifies the account but is
    rejected by the bearer guard, so a leaked link cannot log anyone in."""
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "type": "email_verify", "iat": now, "exp": now + VERIFY_TOKEN_TTL},
                      settings.secret_key, algorithm=settings.algorithm)


def verification_link(token: str) -> str:
    """Built only from APP_BASE_URL (never the request Host)."""
    return build_app_link("/verify-email", token=token)


def _deliver_error(exc: ResendError) -> HTTPException:
    if not resend.is_configured():
        return HTTPException(status_code=503, detail="Email provider is not configured")
    return HTTPException(status_code=502, detail="Email provider error")


@router.post("/send-verification", response_model=EmailSentResponse)
@limiter.limit("3/minute", key_func=user_or_ip_key)
async def send_verification(
    request: Request,
    response: Response,
    payload: SendVerificationRequest,
    current_user: User = Depends(get_current_user),
):
    """Email the caller a 24-hour verification link for their account address.
    The address must be the one on the account."""
    if payload.email.lower() != current_user.email.lower():
        raise HTTPException(status_code=403, detail="You can only verify your own account email")
    link = verification_link(make_verification_token(str(current_user.id)))
    app = settings.app_name
    text = (f"Confirm your {app} email address by opening this link within 24 hours:\n{link}\n\n"
            f"If you did not create a {app} account, ignore this message.")
    body = (f"<p>Confirm your {app} email address by clicking the link below within 24 hours.</p>"
            f'<p><a href="{html.escape(link, quote=True)}">Verify my email</a></p>'
            f"<p>If you did not create a {app} account, ignore this message.</p>")
    try:
        email_id = await resend.send_email(current_user.email, f"Verify your {app} email", body,
                                           text_content=text, template="verification")
    except ResendError as exc:
        logger.warning("verification email not sent for %s: %s", current_user.id, exc)
        raise _deliver_error(exc)
    return EmailSentResponse(sent=True, email_id=email_id)


@router.post("/send-notification", response_model=EmailSentResponse)
@limiter.limit("10/minute", key_func=user_or_ip_key)
async def send_notification(
    request: Request,
    response: Response,
    payload: SendNotificationRequest,
    current_user: User = Depends(get_current_user),
):
    """Send a plain notification email. Users may only notify their own
    address; admins may notify any address. The message is sent as text and
    HTML-escaped, never rendered as markup."""
    if payload.email.lower() != current_user.email.lower() and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="You can only send notifications to your own email")
    safe = html.escape(payload.message).replace("\n", "<br>")
    try:
        email_id = await resend.send_email(str(payload.email), payload.subject, f"<p>{safe}</p>",
                                           text_content=payload.message, template="notification")
    except ResendError as exc:
        logger.warning("notification email not sent by %s: %s", current_user.id, exc)
        raise _deliver_error(exc)
    return EmailSentResponse(sent=True, email_id=email_id)
