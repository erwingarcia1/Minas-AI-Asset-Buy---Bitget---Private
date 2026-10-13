"""Sign-in with a second factor: POST /api/auth/login answers 202 for an
account with two-factor on, and /login/mfa finishes the session.

The contract for everyone else is untouched: the first test re-asserts the
exact 200 body and cookies of a plain login.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

from api.auth import hash_password
from api.config import settings
from api.models import User
from api.routes.auth import COOKIE_PATH, CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
from api.services import twofactor
from api.services.totp import totp_at

PW = "Mfa-Passw0rd!1"
STEP = 30


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(twofactor, "_clock", lambda: c.now)
    return c


@pytest.fixture
async def account(db):
    u = User(email=f"mfa-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified")
    db.add(u)
    await db.commit()
    from api.services.redis_client import get_redis
    await get_redis().delete(f"login-lockout:{u.email}", f"login-fails:{u.email}")
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def notices(monkeypatch):
    """Records the two-factor notices the routes queue instead of emailing."""
    sent = []

    async def fake(email, kind, **ctx):
        sent.append((email, kind, ctx))

    import api.routes.twofactor as tf_routes
    monkeypatch.setattr(tf_routes, "send_two_factor_notice", fake)
    return sent


async def enable(db, user, clock):
    secret, _ = await twofactor.begin_enrolment(db, user)
    codes = await twofactor.confirm_enrolment(db, user, totp_at(secret, clock.now))
    clock.advance(STEP)
    return secret, codes


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def cookie_attrs(response, name):
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}=")).lower()


async def test_a_login_without_two_factor_is_byte_for_byte_unchanged(account):
    async with client() as c:
        r = await c.post("/api/auth/login", json={"email": account.email, "password": PW})
    assert r.status_code == 200
    assert set(r.json()) == {"access_token", "token_type", "expires_in", "csrf_token"}
    refresh = cookie_attrs(r, REFRESH_COOKIE)
    assert "httponly" in refresh and "samesite=lax" in refresh and f"path={COOKIE_PATH}" in refresh
    assert r.cookies[CSRF_COOKIE] == r.json()["csrf_token"]


async def test_a_login_with_two_factor_on_answers_202_and_no_session(db, account, clock):
    await enable(db, account, clock)
    async with client() as c:
        r = await c.post("/api/auth/login", json={"email": account.email, "password": PW})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["mfa_required"] is True and body["expires_in"] == 300 and set(body) == {"mfa_required", "mfa_token", "expires_in"}
    assert "access_token" not in body
    assert r.headers.get_list("set-cookie") == [], "no cookie until the second factor"
    decoded = jwt.decode(body["mfa_token"], settings.secret_key, algorithms=[settings.algorithm])
    assert decoded["type"] == "mfa_pending" and decoded["sub"] == str(account.id)


async def test_the_pending_token_is_not_a_credential(db, account, clock):
    await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})).status_code == 401
        verify = await c.post("/api/auth/verify", json={"token": token})
        assert verify.status_code == 200 and verify.json()["valid"] is False


async def test_the_right_code_finishes_a_session_identical_to_a_plain_login(db, account, clock):
    secret, _ = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        r = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"access_token", "token_type", "expires_in", "csrf_token"}
        refresh = cookie_attrs(r, REFRESH_COOKIE)
        assert "httponly" in refresh and "samesite=lax" in refresh and f"path={COOKIE_PATH}" in refresh
        assert c.cookies[CSRF_COOKIE] == body["csrf_token"]
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {body['access_token']}"})).status_code == 200
        refreshed = await c.post("/api/auth/refresh", headers={CSRF_HEADER: body["csrf_token"]})
        assert refreshed.status_code == 200, "a session minted after the second factor refreshes like any other"


async def test_a_wrong_code_is_refused_without_cookies_and_the_token_survives(db, account, clock):
    secret, _ = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        bad = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "000000"})
        assert bad.status_code == 401 and bad.headers.get_list("set-cookie") == []
        assert "not accepted" in bad.json()["detail"]
        good = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})
        assert good.status_code == 200, "one typo does not force the customer to start over"


async def test_a_code_cannot_be_replayed_across_sign_ins(db, account, clock):
    secret, _ = await enable(db, account, clock)
    code = totp_at(secret, clock.now)
    async with client() as c:
        first = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        assert (await c.post("/api/auth/login/mfa", json={"mfa_token": first, "code": code})).status_code == 200
        second = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        assert (await c.post("/api/auth/login/mfa", json={"mfa_token": second, "code": code})).status_code == 401


async def test_the_pending_token_is_redeemable_once(db, account, clock):
    secret, _ = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        assert (await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})).status_code == 200
        clock.advance(STEP)
        again = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})
        assert again.status_code == 401 and "Start again" in again.json()["detail"]


async def test_a_recovery_code_works_once_and_the_owner_is_told(db, account, clock, notices):
    _, codes = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        r = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "recovery_code": codes[0].lower()})
        assert r.status_code == 200, r.text
        token2 = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        assert (await c.post("/api/auth/login/mfa", json={"mfa_token": token2, "recovery_code": codes[0]})).status_code == 401
    assert [(e, k) for e, k, _ in notices] == [(account.email, "recovery_code_used")]
    assert notices[0][2]["remaining"] == 9


async def test_sending_both_or_neither_factor_is_a_validation_error(db, account, clock):
    await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        assert (await c.post("/api/auth/login/mfa", json={"mfa_token": token})).status_code == 422
        assert (await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "123456", "recovery_code": "x"})).status_code == 422


async def test_an_expired_or_foreign_token_is_refused(db, account, clock):
    await enable(db, account, clock)
    then = datetime.now(timezone.utc) - timedelta(minutes=10)
    expired = jwt.encode({"sub": str(account.id), "type": "mfa_pending", "jti": uuid.uuid4().hex, "pv": "x",
                          "iat": then, "exp": then + timedelta(minutes=5)}, settings.secret_key, algorithm=settings.algorithm)
    not_mfa = jwt.encode({"sub": str(account.id), "type": "refresh", "jti": uuid.uuid4().hex,
                          "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                         settings.secret_key, algorithm=settings.algorithm)
    async with client() as c:
        for token in (expired, not_mfa, "not-a-token-at-all-but-long-enough"):
            r = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "123456"})
            assert r.status_code == 401, token


async def test_the_token_dies_with_the_password_it_was_issued_against(db, account, clock):
    secret, _ = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        account.password_hash = hash_password("Another-Passw0rd!2")
        await db.commit()
        r = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})
        assert r.status_code == 401


async def test_five_wrong_codes_lock_the_account_like_five_wrong_passwords(db, account, clock):
    secret, _ = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        for _ in range(5):
            assert (await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "000000"})).status_code == 401
        sixth = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})
        assert sixth.status_code in (401, 429), "the token is spent after five attempts and the account is locked"
        locked = await c.post("/api/auth/login", json={"email": account.email, "password": PW})
        assert locked.status_code == 429 and "Retry-After" in locked.headers


async def test_a_suspended_account_cannot_finish_the_sign_in(db, account, clock):
    secret, _ = await enable(db, account, clock)
    async with client() as c:
        token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
        account.suspended = True
        await db.commit()
        r = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock.now)})
        assert r.status_code == 403


async def test_a_session_from_before_enrolment_keeps_working(db, account, clock):
    """Turning 2FA on protects future sign-ins; it does not evict the customer who just turned it on."""
    async with client() as c:
        before = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        await enable(db, account, clock)
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {before['access_token']}"})).status_code == 200


async def test_nothing_secret_reaches_the_log(db, account, clock, caplog):
    secret, codes = await enable(db, account, clock)
    code = totp_at(secret, clock.now)
    with caplog.at_level("DEBUG"):
        async with client() as c:
            token = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()["mfa_token"]
            await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "000000"})
            await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": code})
    text = caplog.text
    for forbidden in (secret, token, code, codes[0]):
        assert forbidden not in text
