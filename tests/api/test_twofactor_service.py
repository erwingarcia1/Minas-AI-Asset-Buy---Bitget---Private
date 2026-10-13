"""Two-factor enrolment, verification and recovery codes (api/services/twofactor.py).

Ported from Guardian's tests/test_twofactor.py (G-28, #32) onto the platform's
async service. Organised by attack, like Guardian's:

| Attack | Defence |
|---|---|
| Lock a customer out during enrolment | nothing is in force until a code has been verified |
| Replace a live enrolment from a stolen session | a confirmed enrolment is never overwritten |
| Replay a code read over a shoulder | each step is spent once, atomically |
| Read the shared secret out of the database | encrypted with the platform cipher |
| Reuse a recovery code | single-use, hashed at rest |
| Mint recovery codes from a recovery code | regeneration needs an authenticator code |
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from api.models import User
from api.models.security import UserTotp, UserTotpRecoveryCode
from api.services import twofactor
from api.services.crypto import decrypt_value
from api.services.totp import hash_recovery_code, totp_at

STEP = 30


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(twofactor, "_clock", lambda: c.now)
    return c


async def enrol(db, user, clock: Clock):
    secret, _uri = await twofactor.begin_enrolment(db, user)
    codes = await twofactor.confirm_enrolment(db, user, totp_at(secret, clock.now))
    assert codes is not None
    return secret, codes


# --- enrolment ----------------------------------------------------------------


async def test_two_factor_is_not_in_force_until_the_enrolment_is_confirmed(db, user, clock):
    """A secret is stored so the customer can scan it, but a mis-scanned QR
    must not lock them out: nothing counts until a code has been verified."""
    secret, uri = await twofactor.begin_enrolment(db, user)
    assert uri.startswith("otpauth://totp/") and f"secret={secret}" in uri
    assert await twofactor.is_enabled(db, user.id) is False
    state = await twofactor.status(db, user.id)
    assert state.pending_enrolment is True and state.enabled is False and state.recovery_codes_remaining == 0


async def test_a_wrong_code_during_enrolment_changes_nothing(db, user, clock):
    await twofactor.begin_enrolment(db, user)
    assert await twofactor.confirm_enrolment(db, user, "000000") is None
    assert await twofactor.is_enabled(db, user.id) is False


async def test_confirming_switches_it_on_and_issues_ten_recovery_codes(db, user, clock):
    secret, codes = await enrol(db, user, clock)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert await twofactor.is_enabled(db, user.id) is True
    state = await twofactor.status(db, user.id)
    assert state.enabled and state.enrolled_at is not None and state.recovery_codes_remaining == 10
    assert state.pending_enrolment is False


async def test_a_fresh_enrolment_cannot_overwrite_a_live_one(db, user, clock):
    """Otherwise whoever holds the session replaces the customer's authenticator
    with their own; 2FA has to be disabled (with the factor) first."""
    await enrol(db, user, clock)
    with pytest.raises(twofactor.AlreadyEnabled):
        await twofactor.begin_enrolment(db, user)


async def test_restarting_an_unconfirmed_enrolment_is_allowed(db, user, clock):
    """The customer who mis-scanned simply starts again; only the latest secret counts."""
    first, _ = await twofactor.begin_enrolment(db, user)
    second, _ = await twofactor.begin_enrolment(db, user)
    assert first != second
    assert await twofactor.confirm_enrolment(db, user, totp_at(first, clock.now)) is None
    assert await twofactor.confirm_enrolment(db, user, totp_at(second, clock.now)) is not None


async def test_the_secret_is_encrypted_at_rest(db, user, clock):
    secret, _ = await enrol(db, user, clock)
    row = (await db.execute(select(UserTotp).where(UserTotp.user_id == user.id))).scalar_one()
    assert secret not in row.encrypted_secret
    assert decrypt_value(row.encrypted_secret) == secret


# --- codes and replay -----------------------------------------------------------


async def test_the_current_code_is_accepted_exactly_once(db, user, clock):
    secret, _ = await enrol(db, user, clock)
    clock.advance(STEP)   # the confirmation spent the enrolment step
    code = totp_at(secret, clock.now)
    assert await twofactor.check_code(db, user, code) is True
    assert await twofactor.check_code(db, user, code) is False, "replay"


async def test_an_older_step_is_refused_once_a_newer_one_was_spent(db, user, clock):
    secret, _ = await enrol(db, user, clock)
    clock.advance(2 * STEP)
    assert await twofactor.check_code(db, user, totp_at(secret, clock.now)) is True
    assert await twofactor.check_code(db, user, totp_at(secret, clock.now - STEP)) is False


async def test_the_next_step_still_works_after_this_one_is_spent(db, user, clock):
    secret, _ = await enrol(db, user, clock)
    clock.advance(STEP)
    assert await twofactor.check_code(db, user, totp_at(secret, clock.now)) is True
    clock.advance(STEP)
    assert await twofactor.check_code(db, user, totp_at(secret, clock.now)) is True


async def test_a_distant_step_is_refused(db, user, clock):
    secret, _ = await enrol(db, user, clock)
    clock.advance(STEP)
    assert await twofactor.check_code(db, user, totp_at(secret, clock.now + 5 * STEP)) is False


async def test_two_requests_spending_the_same_code_get_exactly_one_success(db, user, clock):
    """The UPDATE that records the step is the lock: whichever request loses
    the race sees rowcount 0 and refuses, whatever the isolation level."""
    from api.database import AsyncSessionLocal
    secret, _ = await enrol(db, user, clock)
    clock.advance(STEP)
    code = totp_at(secret, clock.now)

    async def attempt():
        async with AsyncSessionLocal() as session:
            u = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            return await twofactor.check_code(session, u, code)

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]


# --- recovery codes ------------------------------------------------------------


async def test_a_recovery_code_works_once_in_any_formatting(db, user, clock):
    _, codes = await enrol(db, user, clock)
    sloppy = codes[0].lower().replace("-", " ")
    verdict = await twofactor.check_second_factor(db, user, recovery_code=sloppy)
    assert verdict.ok and verdict.used_recovery_code and verdict.recovery_codes_remaining == 9
    assert (await twofactor.check_second_factor(db, user, recovery_code=codes[0])).ok is False, "single use"
    assert (await twofactor.status(db, user.id)).recovery_codes_remaining == 9


async def test_recovery_codes_are_hashed_at_rest(db, user, clock):
    _, codes = await enrol(db, user, clock)
    rows = (await db.execute(select(UserTotpRecoveryCode).where(UserTotpRecoveryCode.user_id == user.id))).scalars().all()
    stored = {r.code_hash for r in rows}
    assert stored == {hash_recovery_code(c) for c in codes}
    for code in codes:
        assert code not in stored and code.replace("-", "") not in stored


async def test_another_users_recovery_code_fails(db, user, clock):
    _, codes = await enrol(db, user, clock)
    other = User(email=f"other-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", kyc_status="verified")
    db.add(other)
    await db.commit()
    try:
        await enrol(db, other, clock)
        assert (await twofactor.check_second_factor(db, other, recovery_code=codes[0])).ok is False
        assert (await twofactor.status(db, user.id)).recovery_codes_remaining == 10
    finally:
        await db.delete(other)
        await db.commit()


async def test_regenerating_needs_an_authenticator_code_and_retires_the_old_set(db, user, clock):
    secret, old = await enrol(db, user, clock)
    clock.advance(STEP)
    assert await twofactor.regenerate_recovery_codes(db, user, old[0]) is None, "a recovery code cannot mint codes"
    new = await twofactor.regenerate_recovery_codes(db, user, totp_at(secret, clock.now))
    assert new is not None and len(new) == 10 and not set(new) & set(old)
    assert (await twofactor.check_second_factor(db, user, recovery_code=old[1])).ok is False
    assert (await twofactor.check_second_factor(db, user, recovery_code=new[1])).ok is True


# --- disabling ------------------------------------------------------------------


async def test_disabling_requires_the_factor_and_removes_everything(db, user, clock):
    secret, codes = await enrol(db, user, clock)
    clock.advance(STEP)
    assert await twofactor.disable(db, user, code="000000") is False
    assert await twofactor.is_enabled(db, user.id) is True
    assert await twofactor.disable(db, user, code=totp_at(secret, clock.now)) is True
    assert await twofactor.is_enabled(db, user.id) is False
    assert (await twofactor.status(db, user.id)).recovery_codes_remaining == 0
    assert (await db.execute(select(UserTotpRecoveryCode).where(UserTotpRecoveryCode.user_id == user.id))).first() is None


async def test_disabling_with_a_recovery_code_is_allowed(db, user, clock):
    """The customer whose phone is gone must still be able to turn it off and re-enrol."""
    _, codes = await enrol(db, user, clock)
    assert await twofactor.disable(db, user, recovery_code=codes[3]) is True
    assert await twofactor.is_enabled(db, user.id) is False


async def test_admin_reset_removes_the_enrolment_without_a_factor(db, user, clock):
    await enrol(db, user, clock)
    assert await twofactor.admin_reset(db, user.id) is True
    assert await twofactor.is_enabled(db, user.id) is False
    assert await twofactor.admin_reset(db, user.id) is False, "nothing left to remove"


# --- cutover helpers -------------------------------------------------------------


async def test_imported_guardian_enrolments_and_recovery_hashes_keep_working(db, user, clock):
    """Guardian stores the same sha256 of the normalised code, so its hashes
    carry over verbatim; the secret is re-encrypted under the platform cipher."""
    from datetime import datetime, timezone
    secret = "JBSWY3DPEHPK3PXP"
    codes = ["AAAA-BBBB-0001", "CCCC-DDDD-0002"]
    when = datetime(2026, 8, 1, tzinfo=timezone.utc)
    await twofactor.import_enrolment(db, user.id, plaintext_secret=secret, confirmed=True, last_used_step=None,
                                     created_at=when, confirmed_at=when)
    await twofactor.import_recovery_code_hashes(db, user.id, [(hash_recovery_code(codes[0]), when, None),
                                                              (hash_recovery_code(codes[1]), when, when)])
    assert await twofactor.is_enabled(db, user.id) is True
    assert (await twofactor.status(db, user.id)).recovery_codes_remaining == 1
    assert await twofactor.check_code(db, user, totp_at(secret, clock.now)) is True
    assert (await twofactor.check_second_factor(db, user, recovery_code=codes[1])).ok is False, "already used in Guardian"
    assert (await twofactor.check_second_factor(db, user, recovery_code=codes[0])).ok is True
