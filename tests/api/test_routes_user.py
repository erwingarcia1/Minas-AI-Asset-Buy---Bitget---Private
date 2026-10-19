import uuid

import pytest
from sqlalchemy import select

from api.auth import hash_password
from api.models import User
from tests.api.conftest import auth_headers

PW = "Routes-User-Passw0rd!1"


@pytest.fixture
async def other_user(db):
    u = User(email=f"other-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh:
        await db.delete(fresh)
        await db.commit()


async def test_profile_requires_auth(client):
    async with client as c:
        assert (await c.get("/api/user/profile")).status_code == 401


async def test_get_own_profile(client, user):
    async with client as c:
        r = await c.get("/api/user/profile", headers=auth_headers(user))
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == user.email
    assert "password_hash" not in body
    assert body["created_at"].endswith("Z") or "+" in body["created_at"]


async def test_update_email(client, db, user):
    user.password_hash = hash_password(PW)   # changing the address needs the current password (v0.9.2)
    await db.commit()
    new_email = f"renamed-{uuid.uuid4().hex[:8]}@example.com"
    async with client as c:
        r = await c.put("/api/user/profile", json={"email": new_email, "current_password": PW},
                        headers=auth_headers(user))
    assert r.status_code == 200 and r.json()["email"] == new_email


async def test_update_email_conflict(client, db, user, other_user):
    user.password_hash = hash_password(PW)
    await db.commit()
    async with client as c:
        r = await c.put("/api/user/profile", json={"email": other_user.email, "current_password": PW},
                        headers=auth_headers(user))
    assert r.status_code == 400
    assert "already in use" in r.text


async def test_update_nothing_400(client, user):
    async with client as c:
        r = await c.put("/api/user/profile", json={}, headers=auth_headers(user))
    assert r.status_code == 400


async def test_get_user_by_id_and_404(client, user, other_user):
    async with client as c:
        ok = await c.get(f"/api/user/{other_user.id}", headers=auth_headers(user))
        assert ok.status_code == 200 and ok.json()["email"] == other_user.email
        missing = await c.get(f"/api/user/{uuid.uuid4()}", headers=auth_headers(user))
        assert missing.status_code == 404
