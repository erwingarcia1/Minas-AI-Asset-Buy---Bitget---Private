"""Two-factor authentication: enrolment, verification, recovery codes.

Ported from Guardian's sentinel/twofactor.py (G-28, #32) for the platform's
optional 2FA (v0.9.0): the same rules, keyed by users.id, on async SQLAlchemy
and the platform cipher. The codes themselves are api/services/totp.py.

Rules, each of which is a test:

* 2FA is not in force until the customer has confirmed the enrolment with a
  code. A secret is stored first so they can scan it, but a mis-scanned QR
  must not lock them out of the thing protecting their exchange accounts.
* A confirmed enrolment is never overwritten by a fresh one; it has to be
  disabled first, and disabling needs the second factor. 2FA that whoever
  holds the session can replace protects nothing.
* Each 30-second step is accepted exactly once. The UPDATE that records the
  step is the lock: two requests spending the same code at once get exactly
  one success, whatever the database's isolation level.
* Recovery codes are single-use, stored hashed, and spent the same way.
* Nothing here sends email or logs a secret; the routes do the notifying.

`_clock()` is the test seam Guardian's tests pin (issue #72).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.security import UserTotp, UserTotpRecoveryCode
from api.services import totp as _totp
from api.services.crypto import InvalidToken, decrypt_value, encrypt_value


def _clock() -> float:
    return time.time()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AlreadyEnabled(ValueError):
    """A confirmed enrolment exists; disable it before enrolling again."""


@dataclass(frozen=True)
class TwoFactorState:
    enabled: bool
    enrolled_at: Optional[datetime]
    recovery_codes_remaining: int
    pending_enrolment: bool


@dataclass(frozen=True)
class SecondFactorResult:
    ok: bool
    used_recovery_code: bool = False
    recovery_codes_remaining: int = 0


async def _row(db: AsyncSession, user_id) -> Optional[UserTotp]:
    return (await db.execute(select(UserTotp).where(UserTotp.user_id == user_id))).scalar_one_or_none()


def _secret(row: UserTotp) -> str:
    """Empty for a secret that no longer decrypts: every code is then refused
    (totp.verify_code fails closed on an unusable secret)."""
    try:
        return decrypt_value(row.encrypted_secret)
    except InvalidToken:
        return ""


async def _codes_remaining(db: AsyncSession, user_id) -> int:
    return (await db.execute(
        select(func.count(UserTotpRecoveryCode.id)).where(UserTotpRecoveryCode.user_id == user_id,
                                                          UserTotpRecoveryCode.used_at.is_(None)))).scalar_one()


async def is_enabled(db: AsyncSession, user_id) -> bool:
    row = await _row(db, user_id)
    return bool(row and row.confirmed)


async def status(db: AsyncSession, user_id) -> TwoFactorState:
    row = await _row(db, user_id)
    if row is None:
        return TwoFactorState(enabled=False, enrolled_at=None, recovery_codes_remaining=0, pending_enrolment=False)
    if not row.confirmed:
        return TwoFactorState(enabled=False, enrolled_at=None, recovery_codes_remaining=0, pending_enrolment=True)
    return TwoFactorState(enabled=True, enrolled_at=row.confirmed_at,
                          recovery_codes_remaining=await _codes_remaining(db, user_id), pending_enrolment=False)


async def begin_enrolment(db: AsyncSession, user) -> tuple[str, str]:
    """A fresh secret and its otpauth:// URI. Replaces an unconfirmed enrolment,
    never a confirmed one. The secret exists in plaintext only in the return
    value: show it once, never log it."""
    row = await _row(db, user.id)
    if row is not None and row.confirmed:
        raise AlreadyEnabled("two-factor authentication is already on")
    if row is not None:
        await db.delete(row)
    secret = _totp.generate_secret()
    db.add(UserTotp(user_id=user.id, encrypted_secret=encrypt_value(secret), confirmed=False, created_at=_now()))
    await db.commit()
    return secret, _totp.provisioning_uri(secret, user.email)


async def _replace_recovery_codes(db: AsyncSession, user_id) -> list[str]:
    await db.execute(delete(UserTotpRecoveryCode).where(UserTotpRecoveryCode.user_id == user_id))
    codes = _totp.generate_recovery_codes()
    now = _now()
    for code in codes:
        db.add(UserTotpRecoveryCode(id=uuid.uuid4(), user_id=user_id,
                                    code_hash=_totp.hash_recovery_code(code), created_at=now))
    return codes


async def confirm_enrolment(db: AsyncSession, user, code: str) -> Optional[list[str]]:
    """Switch 2FA on once the customer proves they can produce a code.
    Returns the recovery codes (shown once), or None when the code is wrong
    or nothing is pending."""
    row = await _row(db, user.id)
    if row is None or row.confirmed:
        return None
    step = _totp.verify_code(_secret(row), code, at=_clock(), last_used_step=row.last_used_step)
    if step is None:
        return None
    # One conditional UPDATE is the lock: two requests carrying the same code must
    # not both confirm, or the loser would overwrite the winner's recovery codes
    # after they were shown.
    result = await db.execute(
        update(UserTotp)
        .where(UserTotp.user_id == user.id, UserTotp.confirmed.is_(False))
        .values(confirmed=True, confirmed_at=_now(), last_used_step=step))
    if result.rowcount != 1:
        await db.rollback()
        return None
    codes = await _replace_recovery_codes(db, user.id)
    await db.commit()
    return codes


async def _spend_step(db: AsyncSession, user_id, step: int) -> bool:
    """Record the step as used, atomically. rowcount 0 means another request
    spent it first (or the enrolment vanished): the code is refused."""
    result = await db.execute(
        update(UserTotp)
        .where(UserTotp.user_id == user_id, UserTotp.confirmed.is_(True),
               or_(UserTotp.last_used_step.is_(None), UserTotp.last_used_step < step))
        .values(last_used_step=step))
    await db.commit()
    return result.rowcount == 1


async def check_code(db: AsyncSession, user, code: Optional[str]) -> bool:
    """A TOTP code against a confirmed enrolment, spent on success."""
    row = await _row(db, user.id)
    if row is None or not row.confirmed:
        return False
    step = _totp.verify_code(_secret(row), code, at=_clock(), last_used_step=row.last_used_step)
    if step is None:
        return False
    return await _spend_step(db, user.id, step)


async def check_recovery_code(db: AsyncSession, user, code: Optional[str]) -> bool:
    """A recovery code, spent on success (used_at set, row kept)."""
    if not code or not await is_enabled(db, user.id):
        return False
    result = await db.execute(
        update(UserTotpRecoveryCode)
        .where(UserTotpRecoveryCode.user_id == user.id,
               UserTotpRecoveryCode.code_hash == _totp.hash_recovery_code(code),
               UserTotpRecoveryCode.used_at.is_(None))
        .values(used_at=_now()))
    await db.commit()
    return result.rowcount == 1


async def check_second_factor(db: AsyncSession, user, *, code: Optional[str] = None,
                              recovery_code: Optional[str] = None) -> SecondFactorResult:
    """Exactly one of `code` / `recovery_code`. Never raises on bad input.
    Whitespace counts as absent, so a blank `code` beside a recovery code
    does not shadow it."""
    code = (code or "").strip() or None
    recovery_code = (recovery_code or "").strip() or None
    if code:
        return SecondFactorResult(ok=await check_code(db, user, code))
    if recovery_code and await check_recovery_code(db, user, recovery_code):
        return SecondFactorResult(ok=True, used_recovery_code=True,
                                  recovery_codes_remaining=await _codes_remaining(db, user.id))
    return SecondFactorResult(ok=False)


async def regenerate_recovery_codes(db: AsyncSession, user, code: Optional[str]) -> Optional[list[str]]:
    """New recovery codes against a TOTP code (a recovery code cannot mint
    more recovery codes). The previous set stops working."""
    if not await check_code(db, user, code):
        return None
    codes = await _replace_recovery_codes(db, user.id)
    await db.commit()
    return codes


async def disable(db: AsyncSession, user, *, code: Optional[str] = None,
                  recovery_code: Optional[str] = None) -> bool:
    """Turn 2FA off: needs the second factor, removes the enrolment and every
    recovery code. False when nothing is enabled or the factor is wrong."""
    if not await is_enabled(db, user.id):
        return False
    verdict = await check_second_factor(db, user, code=code, recovery_code=recovery_code)
    if not verdict.ok:
        return False
    await _remove(db, user.id)
    return True


async def _remove(db: AsyncSession, user_id) -> None:
    await db.execute(delete(UserTotpRecoveryCode).where(UserTotpRecoveryCode.user_id == user_id))
    await db.execute(delete(UserTotp).where(UserTotp.user_id == user_id))
    await db.commit()


async def admin_reset(db: AsyncSession, user_id) -> bool:
    """Support path when the authenticator is lost: drop the enrolment and the
    codes without a factor. The caller audits it and tells the user.
    Returns whether there was anything to remove."""
    existed = await _row(db, user_id) is not None
    await _remove(db, user_id)
    return existed


async def import_enrolment(db: AsyncSession, user_id, *, plaintext_secret: str, confirmed: bool,
                           last_used_step: Optional[int], created_at: datetime,
                           confirmed_at: Optional[datetime]) -> None:
    """Cutover helper: a Guardian enrolment, re-encrypted under the platform
    cipher. Replaces any existing row for the user."""
    await db.execute(delete(UserTotp).where(UserTotp.user_id == user_id))
    db.add(UserTotp(user_id=user_id, encrypted_secret=encrypt_value(plaintext_secret), confirmed=confirmed,
                    last_used_step=last_used_step, created_at=created_at, confirmed_at=confirmed_at))
    await db.commit()


async def import_recovery_code_hashes(db: AsyncSession, user_id,
                                      hashes: list[tuple[str, datetime, Optional[datetime]]]) -> None:
    """Cutover helper: Guardian's (code_hash, created_at, used_at) rows, which
    use the same sha256 scheme (totp.hash_recovery_code)."""
    await db.execute(delete(UserTotpRecoveryCode).where(UserTotpRecoveryCode.user_id == user_id))
    for code_hash, created_at, used_at in hashes:
        db.add(UserTotpRecoveryCode(id=uuid.uuid4(), user_id=user_id, code_hash=code_hash,
                                    created_at=created_at, used_at=used_at))
    await db.commit()
