"""POST /api/auth/logout-all: every token issued before now stops working."""
import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from api.auth import hash_password
from api.models import User
from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE

PW = "Everywhere-Passw0rd!1"


@pytest.fixture
async def account(db):
    u = User(email=f"all-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_logout_all_kills_the_access_token_the_refresh_cookie_and_clears_cookies(db, account):
    async with client() as c:
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        old_refresh = c.cookies[REFRESH_COOKIE]
        H = {"Authorization": f"Bearer {login['access_token']}"}
        assert (await c.get("/api/user/profile", headers=H)).status_code == 200
        r = await c.post("/api/auth/logout-all", headers=H)
        assert r.status_code == 204
        cleared = [h.lower() for h in r.headers.get_list("set-cookie")]
        assert any(h.startswith(f"{REFRESH_COOKIE.lower()}=") and ("max-age=0" in h or "expires=" in h) for h in cleared)
        assert (await c.get("/api/user/profile", headers=H)).status_code == 401, "the caller's own token dies too"
        replay = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: old_refresh, CSRF_COOKIE: login["csrf_token"]},
                              headers={CSRF_HEADER: login["csrf_token"]})
        assert replay.status_code == 401
    await db.refresh(account)
    assert account.sessions_revoked_at is not None and account.password_changed_at is None


async def test_logout_all_needs_a_bearer_token(account):
    async with client() as c:
        assert (await c.post("/api/auth/logout-all")).status_code == 401


async def test_a_session_started_afterwards_works_and_other_users_are_untouched(db, account):
    other = User(email=f"oth-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified")
    db.add(other)
    await db.commit()
    try:
        async with client() as c:
            theirs = (await c.post("/api/auth/login", json={"email": other.email, "password": PW})).json()
            mine = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
            assert (await c.post("/api/auth/logout-all", headers={"Authorization": f"Bearer {mine['access_token']}"})).status_code == 204
            assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {theirs['access_token']}"})).status_code == 200
            await asyncio.sleep(1.1)   # JWT iat has one-second resolution, like the password-change cutoff
            again = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
            assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {again['access_token']}"})).status_code == 200
    finally:
        await db.delete(other)
        await db.commit()
