"""Sessions: the refresh token lives in an httpOnly cookie, the access token
only in the response body, and the cookie endpoints require a CSRF header."""
import uuid

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

from api.auth import hash_password
from api.config import settings
from api.models import User
from api.routes import auth as auth_mod
from api.routes.auth import COOKIE_PATH, CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE

PW = "Sess-Passw0rd!1"


@pytest.fixture
async def account(db):
    u = User(email=f"sess-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified")
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


def cookie_attrs(response: httpx.Response, name: str) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}=")).lower()


async def test_login_sets_httponly_cookies_and_returns_no_refresh_token_in_the_body(account):
    async with client() as c:
        r = await c.post("/api/auth/login", json={"email": account.email, "password": PW})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "csrf_token"}, "no refresh token in the body"
    refresh_attrs = cookie_attrs(r, REFRESH_COOKIE)
    assert "httponly" in refresh_attrs and "samesite=lax" in refresh_attrs and f"path={COOKIE_PATH}" in refresh_attrs
    assert "max-age=" in refresh_attrs and "secure" not in refresh_attrs, "Secure is off in development (plain http on localhost)"
    csrf_attrs = cookie_attrs(r, CSRF_COOKIE)
    assert "httponly" not in csrf_attrs and "samesite=lax" in csrf_attrs, "the CSRF cookie must be readable by the page"
    assert r.cookies[CSRF_COOKIE] == body["csrf_token"]
    decoded = jwt.decode(r.cookies[REFRESH_COOKIE], settings.secret_key, algorithms=[settings.algorithm])
    assert decoded["type"] == "refresh" and decoded["sub"] == str(account.id)


def test_cookies_are_secure_outside_development(monkeypatch):
    monkeypatch.setattr(settings, "api_env", "production")
    assert auth_mod._cookie_secure() is True
    monkeypatch.setattr(settings, "api_env", "development")
    assert auth_mod._cookie_secure() is False


async def test_refresh_works_from_the_cookie_with_the_csrf_header_and_rotates(account):
    async with client() as c:   # httpx keeps the cookies, like a browser
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        first_cookie = c.cookies[REFRESH_COOKIE]
        r = await c.post("/api/auth/refresh", headers={CSRF_HEADER: login["csrf_token"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"] and "refresh_token" not in body
        assert c.cookies[REFRESH_COOKIE] != first_cookie and c.cookies[CSRF_COOKIE] == body["csrf_token"], "both cookies rotated"
        # the new access token works
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {body['access_token']}"})).status_code == 200
        # the previous refresh cookie is revoked (replay after rotation fails)
        replay = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: first_cookie, CSRF_COOKIE: body["csrf_token"]},
                              headers={CSRF_HEADER: body["csrf_token"]})
        assert replay.status_code == 401


async def test_csrf_header_is_required_and_must_match_the_cookie(account):
    async with client() as c:
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        missing = await c.post("/api/auth/refresh")
        assert missing.status_code == 403 and missing.json()["detail"]["error"] == "csrf_failed"
        wrong = await c.post("/api/auth/refresh", headers={CSRF_HEADER: "not-the-cookie"})
        assert wrong.status_code == 403
        assert (await c.post("/api/auth/logout")).status_code == 403, "logout needs it too"
        assert c.cookies.get(REFRESH_COOKIE), "a failed CSRF check does not touch the session"
        ok = await c.post("/api/auth/refresh", headers={CSRF_HEADER: login["csrf_token"]})
        assert ok.status_code == 200


async def test_refresh_without_a_cookie_is_401(account):
    async with client() as c:
        r = await c.post("/api/auth/refresh", cookies={CSRF_COOKIE: "c"}, headers={CSRF_HEADER: "c"})
    assert r.status_code == 401


async def test_logout_revokes_server_side_and_clears_the_cookies(account):
    async with client() as c:
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        refresh_cookie = c.cookies[REFRESH_COOKIE]
        r = await c.post("/api/auth/logout", headers={CSRF_HEADER: login["csrf_token"]})
        assert r.status_code == 204
        cleared = [h.lower() for h in r.headers.get_list("set-cookie")]
        assert any(h.startswith(f"{REFRESH_COOKIE}=") and ("max-age=0" in h or "expires=" in h) for h in cleared)
        assert any(h.startswith(f"{CSRF_COOKIE}=") for h in cleared)
        assert not c.cookies.get(REFRESH_COOKIE), "the browser drops the cookie"
        # even if someone kept a copy, it is revoked
        again = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: refresh_cookie, CSRF_COOKIE: login["csrf_token"]},
                             headers={CSRF_HEADER: login["csrf_token"]})
        assert again.status_code == 401
        # logging out twice is harmless
        assert (await c.post("/api/auth/logout", cookies={CSRF_COOKIE: "x"}, headers={CSRF_HEADER: "x"})).status_code == 204


async def test_refresh_token_in_a_body_is_ignored(account):
    """Half-migrated clients that still post the token in the body get nothing: the cookie is the only source."""
    async with client() as c:
        await c.post("/api/auth/login", json={"email": account.email, "password": PW})
        token = c.cookies[REFRESH_COOKIE]
        c.cookies.clear()
        r = await c.post("/api/auth/refresh", json={"refresh_token": token}, cookies={CSRF_COOKIE: "c"}, headers={CSRF_HEADER: "c"})
    assert r.status_code == 401
