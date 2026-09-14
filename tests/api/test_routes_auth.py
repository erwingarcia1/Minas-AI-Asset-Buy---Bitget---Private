import uuid
from datetime import timedelta

import pytest

from api.auth import create_access_token
from tests.api.conftest import auth_headers


def email():
    return f"auth-{uuid.uuid4().hex[:8]}@example.com"


PW = "Str0ng!passw0rd"


@pytest.fixture
async def registered(db):
    import httpx
    from sqlalchemy import select
    from api.main import app
    from api.models import User
    em = email()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/register", json={"email": em, "password": PW})
        assert r.status_code == 201, r.text
    yield em
    u = (await db.execute(select(User).where(User.email == em))).scalar_one_or_none()
    if u:
        await db.delete(u)
        await db.commit()


async def test_register_weak_password_422(client):
    async with client as c:
        r = await c.post("/api/auth/register", json={"email": email(), "password": "weakpass"})
    assert r.status_code == 422
    assert "password" in r.text


async def test_register_bad_email_422(client):
    async with client as c:
        r = await c.post("/api/auth/register", json={"email": "not-an-email", "password": PW})
    assert r.status_code == 422


async def test_register_duplicate_email(registered, client):
    from api.main import app
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/register", json={"email": registered, "password": PW})
    assert r.status_code == 400
    assert "already registered" in r.text


async def test_login_flow_and_wrong_password(registered, client):
    async with client as c:
        ok = await c.post("/api/auth/login", json={"email": registered, "password": PW})
        assert ok.status_code == 200
        body = ok.json()
        assert body["token_type"] == "bearer" and body["expires_in"] > 0
        bad = await c.post("/api/auth/login", json={"email": registered, "password": "Wrong1!pass"})
        assert bad.status_code == 401
        assert bad.headers.get("www-authenticate") == "Bearer"


async def test_verify_endpoint(client, user):
    token = create_access_token(str(user.id)).access_token
    async with client as c:
        good = await c.post("/api/auth/verify", json={"token": token})
        assert good.json() == {"valid": True, "user_id": str(user.id)}
        bad = await c.post("/api/auth/verify", json={"token": "garbage"})
        assert bad.json()["valid"] is False


async def test_refresh_with_invalid_token_401(client):
    from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
    async with client as c:
        r = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: "junk", CSRF_COOKIE: "c"}, headers={CSRF_HEADER: "c"})
        assert r.status_code == 401
        assert (await c.post("/api/auth/refresh", json={"refresh_token": "junk"})).status_code == 403, "no CSRF header, no cookie: refused before anything else"


async def test_expired_token_rejected(client, user):
    expired = create_access_token(str(user.id), expires_delta=timedelta(seconds=-5))
    async with client as c:
        r = await c.get("/api/user/profile",
                        headers={"Authorization": f"Bearer {expired.access_token}"})
    assert r.status_code == 401


async def test_deleted_user_token_rejected(client, db):
    ghost_headers = auth_headers(type("U", (), {"id": uuid.uuid4()})())
    async with client as c:
        r = await c.get("/api/user/profile", headers=ghost_headers)
    assert r.status_code == 401
