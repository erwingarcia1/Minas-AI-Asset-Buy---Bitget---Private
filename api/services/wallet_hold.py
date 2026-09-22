"""24-hour hold on new withdrawal destinations after a password reset or change.

An attacker who takes over an account by resetting its password must not be
able to redirect coins the same day. While the hold is active the account
cannot: add or change the wallet address on a purchase, switch a purchase to
withdraw-immediately, save a new address-book wallet, or create an autobuy
purchase to an address this user has never used. Purchases that already send
to a known address keep running, and a known address is always accepted.

The hold is stored on the user row (migration 022), so it survives restarts
and every worker sees it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Purchase, User
from api.models.purchase import Wallet

logger = logging.getLogger("autobuy.wallet_hold")

HOLD_HOURS = 24
ERROR = "wallet_change_hold"
REASONS = {"password_reset": "a password reset", "password_change": "a password change"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_hold(user: User, reason: str, now: Optional[datetime] = None) -> datetime:
    """Begin (or extend) the hold; the caller commits. Returns the end time."""
    now = now or _now()
    until = now + timedelta(hours=HOLD_HOURS)
    if user.wallet_hold_until is None or user.wallet_hold_until < until:
        user.wallet_hold_until = until
    user.wallet_hold_reason = reason
    logger.warning("wallet change hold started: user=%s reason=%s until=%s", user.id, reason, until.isoformat())
    return user.wallet_hold_until


def active_hold(user: User, now: Optional[datetime] = None) -> Optional[datetime]:
    """The hold's end time while it is active, else None."""
    until = user.wallet_hold_until
    if until is None:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until if until > (now or _now()) else None


def hold_message(user: User, until: datetime) -> str:
    why = REASONS.get(user.wallet_hold_reason or "", "a security event")
    return (f"For your safety, new withdrawal addresses are blocked for {HOLD_HOURS} hours after {why}. "
            f"You can do this after {until.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
            "Purchases to addresses you already use keep running.")


def reject(user: User, until: datetime, action: str) -> HTTPException:
    return HTTPException(status_code=422, detail={
        "error": ERROR,
        "details": {"action": action, "hold_until": until.astimezone(timezone.utc).isoformat(),
                    "reason": user.wallet_hold_reason, "message": hold_message(user, until)}})


async def address_used_before(db: AsyncSession, user_id, address: str) -> bool:
    """True when this user already sends to `address`: it is on one of their
    purchases (any status) or saved in their address book."""
    addr = (address or "").strip()
    if not addr:
        return False
    on_purchase = (await db.execute(select(func.count(Purchase.id)).where(
        Purchase.user_id == user_id, func.lower(Purchase.wallet_address) == addr.lower()))).scalar_one()
    if on_purchase:
        return True
    saved = (await db.execute(select(func.count(Wallet.id)).where(
        Wallet.user_id == user_id, func.lower(Wallet.address) == addr.lower()))).scalar_one()
    return bool(saved)


async def check_new_address(db: AsyncSession, user: User, address: Optional[str], action: str) -> None:
    """Raise 422 wallet_change_hold when the hold is active and `address` is new to this user."""
    until = active_hold(user)
    if until is None or not address:
        return
    if await address_used_before(db, user.id, address):
        return
    logger.warning("wallet change blocked by hold: user=%s action=%s until=%s", user.id, action, until.isoformat())
    raise reject(user, until, action)


def check_action(user: User, action: str) -> None:
    """Raise 422 wallet_change_hold for actions blocked outright during the hold
    (switching a purchase to withdraw-immediately)."""
    until = active_hold(user)
    if until is None:
        return
    logger.warning("action blocked by wallet change hold: user=%s action=%s until=%s", user.id, action, until.isoformat())
    raise reject(user, until, action)


async def notify_hold_lifted(db: AsyncSession, user: User, admin: User, reason: str,
                             was_until: datetime, lifted_at: datetime) -> bool:
    """Security email (never optional) telling the user an admin lifted the hold,
    recorded in the notifications audit table. Never raises."""
    from api.config import settings
    from api.models import Notification
    from api.services.email_service import email_service
    app = settings.app_name
    stamp = lifted_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    contact = f"contact {settings.support_email} immediately" if settings.support_email else "contact support immediately"
    subject = f"Your {app} withdrawal-address hold was lifted"
    text = (f"A {app} support administrator lifted the hold on new withdrawal addresses for your account at {stamp}, "
            f"after you confirmed your identity. New wallet addresses, switching a purchase to withdraw-immediately "
            f"and autobuy purchases to new addresses are allowed again. "
            f"If you did not ask for this, {contact}.")
    html = (f"<p>A {app} support administrator lifted the hold on new withdrawal addresses for your account at "
            f"<strong>{stamp}</strong>, after you confirmed your identity.</p>"
            f"<p>New wallet addresses, switching a purchase to withdraw-immediately and autobuy purchases to new "
            f"addresses are allowed again.</p><p>If you did not ask for this, {contact}.</p>")
    # Security category: queued through the outbox (6 attempts over ~15 minutes); the audit row
    # records "queued" rather than "sent", because delivery now happens on the next drain.
    ok = await email_service.queue(user.email, subject, html, text, template="wallet_hold_lifted",
                                   category="security", user_id=user.id)
    if not ok:
        logger.error("wallet hold lifted notice not delivered: user=%s (outbox unavailable and inline attempt failed)", user.id)
    try:
        db.add(Notification(user_id=user.id, channel="email", template="wallet_hold_lifted", subject=subject,
                            status="queued" if ok else "failed"))
        await db.commit()
    except Exception:
        logger.exception("could not record the wallet-hold-lifted notification for user %s", user.id)
    logger.warning("wallet change hold lifted: user=%s by admin=%s reason=%r was_until=%s",
                   user.id, admin.id, reason, was_until.isoformat())
    return ok
