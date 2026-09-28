"""Private-beta signup gate: registration needs the invite code (constant-time
compared) whenever one is configured, and always in production; the refusal
never says whether the code or the email was the problem; existing users and
the admin signups-per-day count are unaffected / available."""
import uuid

import pytest
from sqlalchemy import select

from api.config import settings
from api.models import User
from tests.api.conftest import auth_headers

PASSWORD = "Str0ng-Passw0rd!!"


async def _cleanup(db, email):
    u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if u:
        await db.delete(u)
        await db.commit()


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "signup_invite_code", "beta-2026-XYZ")
    monkeypatch.setattr(settings, "support_email", "support@minastech.ca")
    from api.services.rate_limit import limiter
    limiter.reset()
    yield
    limiter.reset()


async def test_open_in_development_when_no_code_is_configured(client, db, monkeypatch):
    monkeypatch.setattr(settings, "signup_invite_code", "")
    monkeypatch.setattr(settings, "api_env", "development")
    email = f"open-{uuid.uuid4().hex[:8]}@example.com"
    async with client as c:
        r = await c.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201
    await _cleanup(db, email)


async def test_production_with_no_code_configured_refuses_everyone(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_invite_code", "")
    monkeypatch.setattr(settings, "api_env", "production")
    monkeypatch.setattr(settings, "support_email", "support@minastech.ca")
    async with client as c:
        r = await c.post("/api/auth/register", json={"email": "x@example.com", "password": PASSWORD, "invite_code": "anything"})
    assert r.status_code == 403 and r.json()["detail"] == "Signup is by invitation. Contact support@minastech.ca."


async def test_missing_and_wrong_codes_get_the_same_vague_refusal(client, db, gated):
    email = f"gate-{uuid.uuid4().hex[:8]}@example.com"
    async with client as c:
        missing = await c.post("/api/auth/register", json={"email": email, "password": PASSWORD})
        wrong = await c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": "beta-2026-XYz"})
        # an already-registered email with a wrong code gets the very same answer: the email is not revealed
        db.add(User(email=email, password_hash="x")); await db.commit()
        taken = await c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": "nope"})
    assert missing.status_code == wrong.status_code == taken.status_code == 403
    assert missing.json() == wrong.json() == taken.json() == {"detail": "Signup is by invitation. Contact support@minastech.ca."}
    await _cleanup(db, email)


async def test_correct_code_registers_and_existing_users_are_unaffected(client, db, gated, user):
    email = f"ok-{uuid.uuid4().hex[:8]}@example.com"
    async with client as c:
        r = await c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": "beta-2026-XYZ"})
        assert r.status_code == 201 and r.json()["email"] == email
        # an existing account keeps working with no code anywhere
        me = await c.get("/api/user/profile", headers=auth_headers(user))
        assert me.status_code == 200 and me.json()["email"] == user.email
        # the same email again, with the right code, is the ordinary 400
        again = await c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": "beta-2026-XYZ"})
        assert again.status_code == 400
    await _cleanup(db, email)


async def test_invite_code_never_reaches_the_log(client, db, gated, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    email = f"log-{uuid.uuid4().hex[:8]}@example.com"
    async with client as c:
        await c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": "beta-2026-XYz"})
        await c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": "beta-2026-XYZ"})
    everything = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "beta-2026" not in everything
    assert "signup_invite_used" in everything
    await _cleanup(db, email)


async def test_admin_stats_count_signups_per_day_from_created_at(client, db):
    from tests.api.test_admin import admin as _admin   # noqa: F401  (fixture shape)
    a = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(a); await db.commit()
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    async with client as c:
        r = await c.get("/api/admin/stats", headers=auth_headers(a))
    assert r.status_code == 200
    days = r.json()["signups_by_day"]
    assert len(days) == 14 and days[-1]["day"] == today
    assert days[-1]["count"] >= 1, "the admin created just now counts for today"
    assert all(set(d) == {"day", "count"} for d in days)
    await db.delete(a); await db.commit()
