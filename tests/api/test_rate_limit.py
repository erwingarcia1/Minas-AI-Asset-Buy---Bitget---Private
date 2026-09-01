import uuid

import httpx
import pytest

from api.services.rate_limit import limiter, user_or_ip_key

PW = "Str0ng!passw0rd"


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def limits_on(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(limiter, "_enabled", True) if hasattr(limiter, "_enabled") else None
    limiter.enabled = True
    yield
    limiter.enabled = False
    limiter.reset()


async def test_login_rate_limited_with_retry_after(limits_on):
    async with fresh_client() as c:
        for i in range(5):
            r = await c.post("/api/auth/login",
                             json={"email": f"rl-{uuid.uuid4().hex[:6]}@x.co", "password": PW})
            assert r.status_code == 401, f"attempt {i}: {r.status_code}"
        blocked = await c.post("/api/auth/login",
                               json={"email": "rl-final@x.co", "password": PW})
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers
    assert "x-ratelimit-limit" in blocked.headers


async def test_health_exempt_from_limits(limits_on):
    async with fresh_client() as c:
        for _ in range(10):
            assert (await c.get("/health")).status_code == 200


async def test_account_lockout_after_five_failures(db):
    # lockout is independent of the request rate limiter (keyed per email)
    from sqlalchemy import select
    from api.models import User
    email = f"lockme-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with fresh_client() as c:
            r = await c.post("/api/auth/register", json={"email": email, "password": PW})
            assert r.status_code == 201
            for _ in range(5):
                r = await c.post("/api/auth/login",
                                 json={"email": email, "password": "Wrong1!pass"})
                assert r.status_code == 401
            locked = await c.post("/api/auth/login",
                                  json={"email": email, "password": PW})
            assert locked.status_code == 429, "correct password must not bypass lockout"
            assert "locked" in locked.json()["detail"].lower()
            assert int(locked.headers["retry-after"]) > 0
    finally:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u:
            await db.delete(u)
            await db.commit()
        from api.services.redis_client import get_redis
        await get_redis().delete(f"login-lockout:{email}", f"login-fails:{email}")


async def test_successful_login_clears_failure_counter(db):
    from sqlalchemy import select
    from api.models import User
    email = f"clear-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with fresh_client() as c:
            await c.post("/api/auth/register", json={"email": email, "password": PW})
            for _ in range(4):
                await c.post("/api/auth/login", json={"email": email, "password": "Wrong1!pass"})
            ok = await c.post("/api/auth/login", json={"email": email, "password": PW})
            assert ok.status_code == 200
            for _ in range(4):  # counter reset: four more failures still no lockout
                await c.post("/api/auth/login", json={"email": email, "password": "Wrong1!pass"})
            ok2 = await c.post("/api/auth/login", json={"email": email, "password": PW})
            assert ok2.status_code == 200
    finally:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u:
            await db.delete(u)
            await db.commit()


async def test_body_size_guard():
    async with fresh_client() as c:
        r = await c.post("/api/auth/verify", content=b"x" * (1024 * 1024 + 100),
                         headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_spoofed_forwarded_header_ignored():
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
             "headers": [(b"x-forwarded-for", b"1.2.3.4")],
             "client": ("9.9.9.9", 1234)}
    request = Request(scope)
    from slowapi.util import get_remote_address
    assert get_remote_address(request) == "9.9.9.9", \
        "limit keys must come from the socket address, not spoofable headers"
    assert user_or_ip_key(request) == "9.9.9.9"


def test_user_key_from_bearer_token(user):
    from starlette.requests import Request
    from api.auth import create_access_token
    token = create_access_token(str(user.id)).access_token
    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
             "headers": [(b"authorization", f"Bearer {token}".encode())],
             "client": ("9.9.9.9", 1234)}
    assert user_or_ip_key(Request(scope)) == f"user:{user.id}"
