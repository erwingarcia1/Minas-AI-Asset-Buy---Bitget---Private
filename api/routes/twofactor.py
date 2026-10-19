"""Two-factor authentication management: /api/user/2fa (v0.9.0).

Enrol (password required), confirm with a code (switches it on and issues the
recovery codes, shown once), regenerate recovery codes (code required), and
disable (password plus a code or a recovery code). Every state change emails
the account. Sign-in with the second factor is /api/auth/login/mfa.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_password
from api.config import settings
from api.database import get_db
from api.models import User
from api.routes.user import get_current_user
from api.services import twofactor
from api.services.email_service import email_service
from api.services.rate_limit import limiter, register_failed_login, user_or_ip_key
from api.services.telegram import mask_email

logger = logging.getLogger("autobuy.auth")

router = APIRouter()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class TwoFactorStatus(BaseModel):
    enabled: bool
    enrolled_at: Optional[datetime] = None
    recovery_codes_remaining: int = 0
    pending_enrolment: bool = False


class EnrolRequest(BaseModel):
    password: str = Field(max_length=128)


class EnrolResponse(BaseModel):
    """Shown once. The secret never comes back from the API again."""
    secret: str
    otpauth_uri: str


class ConfirmRequest(BaseModel):
    code: str = Field(max_length=32)


class RecoveryCodesResponse(BaseModel):
    """Shown once; the previous codes stop working."""
    recovery_codes: List[str]


class RegenerateRequest(BaseModel):
    code: str = Field(max_length=32)


class DisableRequest(BaseModel):
    password: str = Field(max_length=128)
    code: Optional[str] = Field(default=None, max_length=32)
    recovery_code: Optional[str] = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _exactly_one_factor(self):
        if bool((self.code or "").strip()) == bool((self.recovery_code or "").strip()):
            raise ValueError("send either code or recovery_code")
        return self


# --------------------------------------------------------------------------
# Notices
# --------------------------------------------------------------------------

def _stamp(when: Optional[datetime]) -> str:
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _contact() -> str:
    return f"contact {settings.support_email} immediately" if settings.support_email else "contact support immediately"


async def send_two_factor_notice(email: str, kind: str, when: Optional[datetime] = None, ip: str = "",
                                 remaining: Optional[int] = None) -> None:
    """Security notice for a two-factor change. Never optional, never affects
    the change itself; queued through the outbox (security category)."""
    app = settings.app_name
    where = f"\nFrom IP address: {ip}" if ip else ""
    if kind == "enabled":
        subject = f"Two-factor authentication is on for your {app} account"
        body = (f"Two-factor authentication was turned on for your {app} account.\n\nWhen: {_stamp(when)}{where}\n\n"
                "From now on, signing in needs a code from your authenticator app, or one of the recovery codes "
                "you were shown. Keep those codes somewhere safe.")
    elif kind == "disabled":
        subject = f"Two-factor authentication is off for your {app} account"
        body = (f"Two-factor authentication was turned off for your {app} account.\n\nWhen: {_stamp(when)}{where}\n\n"
                f"Signing in now needs only your password. If this wasn't you, {_contact()} and change your password.")
    elif kind == "codes_regenerated":
        subject = f"New recovery codes for your {app} account"
        body = (f"A new set of recovery codes was generated for your {app} account.\n\nWhen: {_stamp(when)}{where}\n\n"
                f"The previous codes no longer work. If this wasn't you, {_contact()}.")
    elif kind == "recovery_code_used":
        left = f" You have {remaining} left." if remaining is not None else ""
        subject = f"A recovery code was used to sign in to your {app} account"
        body = (f"A recovery code was used instead of an authenticator code to sign in to your {app} account.\n\n"
                f"When: {_stamp(when)}{where}\n\nEach recovery code works once.{left} If this wasn't you, {_contact()}; "
                "if it was, consider generating a new set under Settings > Security.")
    elif kind == "reset_by_admin":
        subject = f"Two-factor authentication was reset on your {app} account"
        body = (f"Support removed the two-factor enrolment from your {app} account at your request.\n\n"
                f"When: {_stamp(when)}\n\nSigning in now needs only your password. Set two-factor authentication up "
                f"again under Settings > Security as soon as you can. If you did not ask for this, {_contact()}.")
    else:
        logger.error("unknown two-factor notice kind %r", kind)
        return
    html = "".join(f"<p>{line}</p>" for line in body.split("\n\n"))
    if not await email_service.queue(email, subject, html, body, template="two_factor", category="security"):
        logger.error("two-factor notice (%s) not delivered to %s: outbox unavailable and inline attempt failed",
                     kind, mask_email(email))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("", response_model=TwoFactorStatus)
async def two_factor_status(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Whether two-factor authentication is on for the signed-in account, when
    it was enabled, how many recovery codes are unused, and whether an
    enrolment is waiting to be confirmed."""
    state = await twofactor.status(db, current_user.id)
    return TwoFactorStatus(enabled=state.enabled, enrolled_at=state.enrolled_at,
                           recovery_codes_remaining=state.recovery_codes_remaining,
                           pending_enrolment=state.pending_enrolment)


@router.post("/enrol", response_model=EnrolResponse)
@limiter.limit("5/minute", key_func=user_or_ip_key)
async def enrol(request: Request, response: Response, payload: EnrolRequest,
                current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Start two-factor enrolment: needs the current password and answers a
    fresh secret with its otpauth:// URI for the authenticator app. Nothing is
    in force until /confirm succeeds. 409 when two-factor is already on (turn
    it off first); starting again replaces an unconfirmed enrolment."""
    if not verify_password(payload.password, current_user.password_hash):
        await register_failed_login(current_user.email)   # a stolen session guessing the password should get loud
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    try:
        secret, uri = await twofactor.begin_enrolment(db, current_user)
    except twofactor.AlreadyEnabled:
        raise HTTPException(status_code=409,
                            detail="Two-factor authentication is already on. Turn it off first to set it up again.")
    logger.info("two_factor_enrolment_started", extra={"user_id": str(current_user.id)})
    return EnrolResponse(secret=secret, otpauth_uri=uri)


@router.post("/confirm", response_model=RecoveryCodesResponse)
@limiter.limit("10/minute", key_func=user_or_ip_key)
async def confirm(request: Request, response: Response, payload: ConfirmRequest, background: BackgroundTasks,
                  current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Prove the authenticator works and switch two-factor on. Answers the ten
    recovery codes, shown this once. 400 when no enrolment is pending or the
    code is wrong."""
    codes = await twofactor.confirm_enrolment(db, current_user, payload.code)
    if codes is None:
        raise HTTPException(status_code=400,
                            detail="That code was not accepted. Scan the QR code again and enter a fresh code.")
    logger.info("two_factor_enabled", extra={"user_id": str(current_user.id)})
    background.add_task(send_two_factor_notice, current_user.email, "enabled",
                        when=datetime.now(timezone.utc), ip=_client_ip(request))
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post("/recovery-codes", response_model=RecoveryCodesResponse)
@limiter.limit("3/minute", key_func=user_or_ip_key)
async def regenerate_recovery_codes(request: Request, response: Response, payload: RegenerateRequest,
                                    background: BackgroundTasks, current_user: User = Depends(get_current_user),
                                    db: AsyncSession = Depends(get_db)):
    """A new set of recovery codes against a current authenticator code (a
    recovery code cannot mint more). The previous set stops working."""
    codes = await twofactor.regenerate_recovery_codes(db, current_user, payload.code)
    if codes is None:
        await register_failed_login(current_user.email)
        raise HTTPException(status_code=400,
                            detail="That code was not accepted. Enter the current code from your authenticator app.")
    logger.info("two_factor_codes_regenerated", extra={"user_id": str(current_user.id)})
    background.add_task(send_two_factor_notice, current_user.email, "codes_regenerated",
                        when=datetime.now(timezone.utc), ip=_client_ip(request))
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post("/disable", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
@limiter.limit("5/minute", key_func=user_or_ip_key)
async def disable(request: Request, response: Response, payload: DisableRequest, background: BackgroundTasks,
                  current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Turn two-factor off: needs the current password and either an
    authenticator code or a recovery code. Refused for admin accounts while
    ADMIN_MFA_REQUIRED is on. A wrong password or code counts towards the
    account lockout; the account is emailed when it succeeds."""
    if settings.admin_mfa_required and current_user.role == "admin":
        raise HTTPException(status_code=403,
                            detail="Two-factor authentication is required for admin accounts and cannot be turned off.")
    if not verify_password(payload.password, current_user.password_hash):
        await register_failed_login(current_user.email)
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if not await twofactor.is_enabled(db, current_user.id):
        raise HTTPException(status_code=400, detail="Two-factor authentication is not on for this account.")
    if not await twofactor.disable(db, current_user, code=payload.code, recovery_code=payload.recovery_code):
        await register_failed_login(current_user.email)
        raise HTTPException(status_code=400, detail="That code was not accepted.")
    logger.info("two_factor_disabled", extra={"user_id": str(current_user.id)})
    background.add_task(send_two_factor_notice, current_user.email, "disabled",
                        when=datetime.now(timezone.utc), ip=_client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
