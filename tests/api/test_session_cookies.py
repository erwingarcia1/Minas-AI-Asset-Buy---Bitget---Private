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
from api.routes.auth import COOKIE_PATH, CSRF_COOKIE, CSRF_COOKIE_PATH, CSRF_HEADER, REFRESH_COOKIE

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
    assert CSRF_COOKIE_PATH == "/" and "path=/;" in csrf_attrs + ";", \
        "the CSRF cookie must be visible to document.cookie on every page route, not only under /api/auth"
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
        # the previous refresh cookie is revoked: inside the 30 s rotation grace a replay
        # gets the SAME session back (a second tab), after it the replay fails
        replay = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: first_cookie, CSRF_COOKIE: body["csrf_token"]},
                              headers={CSRF_HEADER: body["csrf_token"]})
        assert replay.status_code == 200 and replay.json()["access_token"] == body["access_token"]
        from api.routes.auth import GRACE_PREFIX
        from api.services.redis_client import get_redis
        await get_redis().delete(GRACE_PREFIX + jwt.get_unverified_claims(first_cookie)["jti"])   # window over
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
        # logout with a valid refresh cookie succeeds without the header (SameSite=Lax cookies cannot be
        # sent cross-site, and a stale tab must still be able to sign out for real); see test_logout_stale_csrf
        assert (await c.post("/api/auth/logout", cookies={REFRESH_COOKIE: "not-a-token", CSRF_COOKIE: "x"})).status_code == 403, "no valid cookie: the CSRF check still applies"
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


async def test_csrf_failures_are_logged_with_a_reason(account, caplog):
    import logging
    async with client() as c:
        await c.post("/api/auth/login", json={"email": account.email, "password": PW})
        with caplog.at_level(logging.WARNING, logger="autobuy.auth"):
            r = await c.post("/api/auth/refresh")
    assert r.status_code == 403 and "no X-CSRF-Token header" in r.json()["detail"]["details"]["message"]
    rec = next(x for x in caplog.records if x.getMessage() == "csrf_failed")
    assert rec.reason == "no X-CSRF-Token header" and rec.has_refresh_cookie is True and rec.path == "/api/auth/refresh"


async def test_refresh_survives_an_api_restart(account):
    """The signing key comes from SECRET_KEY in the environment, never generated at
    startup, so a refresh cookie minted by one process is valid for the next one."""
    import importlib
    from api import config as config_mod
    assert "secrets.token" not in open(config_mod.__file__).read() and "uuid4" not in open(config_mod.__file__).read()
    fresh = config_mod.Settings()          # what a restarted process would load
    assert fresh.secret_key == settings.secret_key and fresh.algorithm == settings.algorithm
    assert fresh.refresh_token_expire_days >= 1 and fresh.access_token_expire_minutes <= 60
    async with client() as c:
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        cookie = c.cookies[REFRESH_COOKIE]
    # "restart": a brand-new client (no shared state) presenting the cookie minted before
    decoded = jwt.decode(cookie, fresh.secret_key, algorithms=[fresh.algorithm])
    assert decoded["type"] == "refresh"
    async with client() as c2:
        r = await c2.post("/api/auth/refresh", cookies={REFRESH_COOKIE: cookie, CSRF_COOKIE: login["csrf_token"]},
                          headers={CSRF_HEADER: login["csrf_token"]})
    assert r.status_code == 200 and r.json()["access_token"]
    importlib.invalidate_caches()


async def test_refresh_fails_only_after_a_password_change_revocation_or_expiry(account, db):
    import asyncio
    from datetime import datetime, timedelta, timezone
    async with client() as c:
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": PW})).json()
        H = {CSRF_HEADER: login["csrf_token"]}
        # a plain refresh keeps working, repeatedly (rotation)
        for _ in range(3):
            r = await c.post("/api/auth/refresh", headers={CSRF_HEADER: c.cookies[CSRF_COOKIE]})
            assert r.status_code == 200, r.text
        # password change: everything issued before dies
        await asyncio.sleep(1.1)
        acc = r.json()["access_token"]
        ok = await c.post("/api/auth/change-password", headers={"Authorization": f"Bearer {acc}"},
                          json={"current_password": PW, "new_password": "Changed-Passw0rd!2"})
        assert ok.status_code == 204
        r = await c.post("/api/auth/refresh", headers={CSRF_HEADER: c.cookies[CSRF_COOKIE]})
        assert r.status_code == 401, "pre-change refresh cookie must die"
        assert not c.cookies.get(REFRESH_COOKIE), "and the API clears the dead cookie"
        # expiry: a cookie past its exp is rejected
        expired = jwt.encode({"sub": str(account.id), "type": "refresh", "jti": "x1",
                              "iat": datetime.now(timezone.utc) - timedelta(days=8),
                              "exp": datetime.now(timezone.utc) - timedelta(days=1)},
                             settings.secret_key, algorithm=settings.algorithm)
        r = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: expired, CSRF_COOKIE: "c"}, headers={CSRF_HEADER: "c"})
        assert r.status_code == 401
