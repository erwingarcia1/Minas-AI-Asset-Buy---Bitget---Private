"""Accounts that arrive without a password (v0.9.2): members migrated from
Minas AI Guardian and customers an admin onboards.

``provision_user`` creates the platform user with an unusable random password
and ``password_reset_required`` set. The customer chooses a real password
through a seven-day set-password link, which is the ordinary reset flow with
a longer-lived token; ``reset_password`` clears the flag. Until then every
sign-in attempt is refused exactly like a wrong password (api/routes/auth.py)
and quietly re-sends the activation email, at most once an hour per user.

Nothing here logs an address unmasked, a token or a link.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import hash_password
from api.config import settings
from api.models import User
from api.services.email_service import email_service
from api.services.email_templates import render
from api.services.telegram import mask_email

__all__ = ["ACTIVATION_SENT_PREFIX", "ACTIVATION_THROTTLE_S", "ACTIVATION_TTL", "activation_link",
           "maybe_send_activation", "provision_user", "send_activation_email"]

logger = logging.getLogger("autobuy.guardian_identity")

ACTIVATION_TTL = timedelta(days=7)
ACTIVATION_SENT_PREFIX = "activation-reset-sent:"
ACTIVATION_THROTTLE_S = 3600


async def provision_user(db: AsyncSession, email: str, role: str = "user") -> Tuple[User, bool]:
    """The user for `email` (compared lower-case): the existing row with
    ``False``, or a new one with ``True``. A new user gets a random password
    nobody knows, ``password_reset_required`` set, KYC pending and `role`."""
    email = email.strip().lower()
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    user = User(email=email, password_hash=hash_password(secrets.token_urlsafe(32)),
                password_reset_required=True, kyc_status="pending", role=role)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # two provisioning calls raced on the same address: the other one won
        await db.rollback()
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one()
        return existing, False
    await db.refresh(user)
    logger.info("user provisioned without a password", extra={"user_id": str(user.id), "role": role})
    return user, True


def activation_link(user: User) -> str:
    """The seven-day set-password link: the ordinary reset link, longer-lived.
    Built only from APP_BASE_URL, never from a request."""
    from api.routes.auth import make_password_reset_token, password_reset_link   # lazy: auth imports this module
    return password_reset_link(make_password_reset_token(user, ttl=ACTIVATION_TTL))


async def send_activation_email(user: User) -> bool:
    """Queue the activation email (security category: retried by the outbox
    drain). True when queued or sent; never raises."""
    link = activation_link(user)
    html, text = render("guardian_activate", link=link, days=ACTIVATION_TTL.days)
    subject = f"Set your {settings.app_name} password"
    ok = await email_service.queue(user.email, subject, html, text, template="guardian_activate",
                                   category="security", user_id=user.id)
    if not ok:
        logger.error("activation email not delivered to %s: outbox unavailable and inline attempt failed",
                     mask_email(user.email))
    return ok


def _redis():
    from api.services.redis_client import get_redis
    return get_redis()


async def maybe_send_activation(user: User) -> bool:
    """Send the activation email unless one went out in the last hour. The
    throttle is a Redis SET NX; with Redis down nothing is sent and nothing
    is raised (this runs as a background task behind a refused sign-in)."""
    key = ACTIVATION_SENT_PREFIX + str(user.id)
    try:
        claimed = await _redis().set(key, "1", nx=True, ex=ACTIVATION_THROTTLE_S)
    except Exception as exc:  # noqa: BLE001 - Redis down must not surface anywhere
        logger.warning("activation email throttle unavailable (Redis: %s); nothing sent to %s",
                       type(exc).__name__, mask_email(user.email))
        return False
    if not claimed:
        logger.info("activation email already sent within the hour", extra={"user_id": str(user.id)})
        return False
    try:
        return await send_activation_email(user)
    except Exception as exc:  # noqa: BLE001
        logger.error("activation email failed for %s: %s", mask_email(user.email), type(exc).__name__)
        return False
