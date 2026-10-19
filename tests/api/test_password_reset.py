"""Forgot / reset password: token issue, single use, expiry, session invalidation."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

import api.routes.auth as auth_mod
from api.auth import hash_password
from api.config import settings
from api.integrations import resend
from api.models import User
from tests.api.conftest import auth_headers

OLD, NEW = "Old-Passw0rd!", "New-Passw0rd!"


@pytest.fixture
async def account(db):
    u = User(email=f"reset-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(OLD), kyc_status="pending")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def sent(monkeypatch):
    """Security emails are queued to the outbox (audit C2); capture at the outbox."""
    box = []

    async def fake_enqueue(to, subject, html, text, template="generic", *, category="confirmation", user_id=None, now=None):
        box.append({"to": to, "subject": subject, "text": text, "html": html, "template": template, "category": category})
        return True
    from api.services import email_outbox
    monkeypatch.setattr(email_outbox, "enqueue", fake_enqueue)
    return box


def link_token(mail: dict) -> str:
    return mail["text"].split("token=", 1)[1].split()[0]


async def test_forgot_password_sends_link_for_known_email(client, account, sent, monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://app.autobuy.test")
    async with client as c:
        r = await c.post("/api/auth/forgot-password", json={"email": account.email.upper()})
    assert r.status_code == 200 and r.json() == {"sent": True}
    assert len(sent) == 1 and sent[0]["to"] == account.email
    assert sent[0]["subject"] == f"Reset your {settings.app_name} password"
    assert sent[0]["category"] == "security" and sent[0]["template"] == "password_reset"
    assert "https://app.autobuy.test/reset-password?token=" in sent[0]["text"]
    decoded = jwt.decode(link_token(sent[0]), settings.secret_key, algorithms=[settings.algorithm])
    assert decoded["type"] == "password_reset" and decoded["sub"] == str(account.id)
    assert decoded["exp"] - decoded["iat"] == 3600


async def test_forgot_password_does_not_reveal_unknown_emails(client, sent):
    async with client as c:
        r = await c.post("/api/auth/forgot-password", json={"email": "nobody-here@example.com"})
    assert r.status_code == 200 and r.json() == {"sent": True}
    assert sent == []


async def test_reset_token_cannot_be_used_as_a_login(client, account):
    token = auth_mod.make_password_reset_token(account)
    async with client as c:
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})).status_code == 401


async def test_reset_password_flow(client, db, account, sent):
    async with client as c:
        old_session = (await c.post("/api/auth/login", json={"email": account.email, "password": OLD})).json()
        from api.routes.auth import CSRF_COOKIE, REFRESH_COOKIE
        old_refresh = c.cookies[REFRESH_COOKIE]
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {old_session['access_token']}"})).status_code == 200
        await c.post("/api/auth/forgot-password", json={"email": account.email})
        token = link_token(sent[0])

        r = await c.post("/api/auth/reset-password", json={"token": token, "new_password": NEW})
        assert r.status_code == 204, r.text
        # new password works, old does not
        assert (await c.post("/api/auth/login", json={"email": account.email, "password": NEW})).status_code == 200
        assert (await c.post("/api/auth/login", json={"email": account.email, "password": OLD})).status_code == 401
        # every session issued before the reset is signed out
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {old_session['access_token']}"})).status_code == 401
        from api.routes.auth import CSRF_HEADER
        assert (await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: old_refresh, CSRF_COOKIE: old_session["csrf_token"]},
                             headers={CSRF_HEADER: old_session["csrf_token"]})).status_code == 401, \
            "the pre-reset refresh cookie is rejected"
        # the token is single-use
        r = await c.post("/api/auth/reset-password", json={"token": token, "new_password": "Third-Passw0rd!"})
        assert r.status_code == 400 and "Invalid or expired" in r.json()["detail"]


async def test_reset_rejects_bad_tokens_and_weak_passwords(client, account):
    good = auth_mod.make_password_reset_token(account)
    now = datetime.now(timezone.utc)
    expired = jwt.encode({"sub": str(account.id), "type": "password_reset", "jti": "x1", "pv": auth_mod._password_version(account.password_hash),
                          "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)}, settings.secret_key, algorithm=settings.algorithm)
    wrong_type = jwt.encode({"sub": str(account.id), "type": "email_verify", "jti": "x2", "iat": now, "exp": now + timedelta(hours=1)},
                            settings.secret_key, algorithm=settings.algorithm)
    forged = jwt.encode({"sub": str(account.id), "type": "password_reset", "jti": "x3", "pv": "0000000000000000", "iat": now,
                         "exp": now + timedelta(hours=1)}, "not-the-secret", algorithm=settings.algorithm)
    async with client as c:
        for tok in (expired, wrong_type, forged, "garbage-token-value-here"):
            r = await c.post("/api/auth/reset-password", json={"token": tok, "new_password": NEW})
            assert r.status_code == 400, tok[:20]
        # weak / same password
        assert (await c.post("/api/auth/reset-password", json={"token": good, "new_password": "weakpass"})).status_code == 422
        assert (await c.post("/api/auth/reset-password", json={"token": good, "new_password": OLD})).status_code == 422
        # a token issued before a normal password change is dead
        H = auth_headers(account)
        assert (await c.post("/api/auth/change-password", json={"current_password": OLD, "new_password": NEW}, headers=H)).status_code == 204
        assert (await c.post("/api/auth/reset-password", json={"token": good, "new_password": "Another-Passw0rd!"})).status_code == 400


async def test_suspended_accounts_get_no_link(client, db, account, sent):
    account.suspended = True
    await db.commit()
    async with client as c:
        r = await c.post("/api/auth/forgot-password", json={"email": account.email})
    assert r.status_code == 200 and sent == []


# --------------------------------------------------------------------------- /reset-password/validate

async def test_validate_does_not_consume_a_valid_token(client, db, account, sent):
    async with client as c:
        await c.post("/api/auth/forgot-password", json={"email": account.email})
        token = link_token(sent[0])
        for _ in range(3):  # repeated validation must not burn the token
            r = await c.post("/api/auth/reset-password/validate", json={"token": token})
            assert r.status_code == 200 and r.json() == {"valid": True}
        r = await c.post("/api/auth/reset-password", json={"token": token, "new_password": NEW})
        assert r.status_code == 204, "token still usable after validation"


async def test_validate_returns_false_after_a_successful_reset(client, account):
    token = auth_mod.make_password_reset_token(account)
    async with client as c:
        assert (await c.post("/api/auth/reset-password", json={"token": token, "new_password": NEW})).status_code == 204
        r = await c.post("/api/auth/reset-password/validate", json={"token": token})
        assert r.status_code == 400 and r.json() == {"valid": False}


async def test_validate_rejects_garbage_expired_and_wrong_type(client, account):
    now = datetime.now(timezone.utc)
    expired = jwt.encode({"sub": str(account.id), "type": "password_reset", "jti": "v1", "pv": auth_mod._password_version(account.password_hash),
                          "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)}, settings.secret_key, algorithm=settings.algorithm)
    access = auth_headers(account)["Authorization"].split()[1]
    async with client as c:
        for tok in ("not-a-token", expired, access):
            r = await c.post("/api/auth/reset-password/validate", json={"token": tok})
            assert r.status_code == 400 and r.json() == {"valid": False}, tok[:15]
