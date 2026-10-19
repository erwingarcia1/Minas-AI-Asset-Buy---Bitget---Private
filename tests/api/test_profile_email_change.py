"""PUT /api/user/profile (v0.9.2): changing the email address, where security
notices and Guardian alerts go, needs the current password, plus the current
authenticator code when two-factor authentication is on, and the previous
address is told. A wrong password or code counts towards the sign-in lockout,
a locked account is refused with 429 exactly as at /login, and the route is
limited to five calls a minute. A request that does not change the address
needs neither proof.
"""
import uuid

import httpx
import pytest
from sqlalchemy import select

from api.auth import hash_password
from api.config import settings
from api.models import User
from api.services import twofactor
from api.services.totp import totp_at
from tests.api.conftest import auth_headers

PW = "Profile-Passw0rd!1"
NEED_PASSWORD = "Enter your current password to change the email address on this account."
NEED_CODE = "Enter the current code from your authenticator app to change the email address."
CODE_REFUSED = "That code was not accepted. Wait for the next code in your authenticator app and try again."


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def new_address() -> str:
    return f"moved-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
async def account(db):
    u = User(email=f"prof-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified")
    db.add(u)
    await db.commit()
    from api.services.redis_client import get_redis
    await get_redis().delete(f"login-fails:{u.email}", f"login-lockout:{u.email}")
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await get_redis().delete(f"login-fails:{u.email}", f"login-lockout:{u.email}",
                                 f"login-fails:{fresh.email}", f"login-lockout:{fresh.email}")
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def sent(monkeypatch):
    box = []

    async def fake_enqueue(to, subject, html, text, template="generic", *, category="confirmation", user_id=None, now=None):
        box.append({"to": to, "subject": subject, "html": html, "text": text, "template": template, "category": category,
                    "user_id": user_id})
        return True
    from api.services import email_outbox
    monkeypatch.setattr(email_outbox, "enqueue", fake_enqueue)
    return box


async def enable_two_factor(db, user, monkeypatch):
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(twofactor, "_clock", lambda: clock["now"])
    secret, _ = await twofactor.begin_enrolment(db, user)
    assert await twofactor.confirm_enrolment(db, user, totp_at(secret, clock["now"])) is not None
    clock["now"] += 30
    return secret, clock


async def test_changing_the_email_without_the_password_is_refused(db, account, sent):
    target = new_address()
    async with fresh_client() as c:
        r = await c.put("/api/user/profile", json={"email": target}, headers=auth_headers(account))
    assert r.status_code == 400 and r.json()["detail"] == NEED_PASSWORD
    await db.refresh(account)
    assert account.email != target and sent == []


async def test_a_wrong_password_is_refused_and_counted(db, account, sent):
    from api.services.redis_client import get_redis
    async with fresh_client() as c:
        r = await c.put("/api/user/profile", json={"email": new_address(), "current_password": "Wrong-Passw0rd!1"},
                        headers=auth_headers(account))
    assert r.status_code == 400 and r.json()["detail"] == "Current password is incorrect"
    assert await get_redis().get(f"login-fails:{account.email}") == "1", "counted like a failed sign-in"
    assert sent == []


async def test_two_factor_on_requires_the_current_code(db, account, sent, monkeypatch):
    """Two messages: a missing code is asked for; a wrong or already used one
    is refused and counted like a wrong code at /login/mfa."""
    from api.services.redis_client import get_redis
    secret, clock = await enable_two_factor(db, account, monkeypatch)
    target = new_address()
    async with fresh_client() as c:
        H = auth_headers(account)
        missing = await c.put("/api/user/profile", json={"email": target, "current_password": PW}, headers=H)
        assert missing.status_code == 400 and missing.json()["detail"] == NEED_CODE
        assert await get_redis().get(f"login-fails:{account.email}") is None, "asking for the code is not a failure"
        wrong = await c.put("/api/user/profile", json={"email": target, "current_password": PW, "code": "000000"}, headers=H)
        assert wrong.status_code == 400 and wrong.json()["detail"] == CODE_REFUSED
        assert await get_redis().get(f"login-fails:{account.email}") == "1", "a wrong code counts"
        bad_password = await c.put("/api/user/profile", json={"email": target, "current_password": "Nope-Passw0rd!1",
                                                              "code": totp_at(secret, clock["now"])}, headers=H)
        assert bad_password.status_code == 400 and bad_password.json()["detail"] == "Current password is incorrect"
        assert await get_redis().get(f"login-fails:{account.email}") == "2"
        code = totp_at(secret, clock["now"])
        ok = await c.put("/api/user/profile", json={"email": target, "current_password": PW, "code": code}, headers=H)
        assert ok.status_code == 200, ok.text
        assert ok.json()["email"] == target and ok.json()["mfa_enabled"] is True
    assert [m["to"] for m in sent] == [account.email], "the notice goes to the address that was replaced"
    from api.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:   # the app spent the step through its own session
        assert await twofactor.check_code(fresh, account, code) is False, "the step was spent by the email change"


async def test_success_saves_the_new_address_and_tells_the_old_one(db, account, sent, caplog):
    old = account.email
    target = new_address()
    with caplog.at_level("INFO"):
        async with fresh_client() as c:
            r = await c.put("/api/user/profile", json={"email": target, "current_password": PW}, headers=auth_headers(account))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == target and body["mfa_enabled"] is False and body["guardian_enabled"] is False
    assert "password_hash" not in body
    await db.refresh(account)
    assert account.email == target
    assert len(sent) == 1
    mail = sent[0]
    assert mail["to"] == old and mail["template"] == "email_changed" and mail["category"] == "security"
    assert mail["subject"] == f"The email address on your {settings.app_name} account was changed"
    assert mail["user_id"] == str(account.id), "queued against the account it concerns (the outbox keeps ids as text)"
    assert f"{target[:2]}…@example.com" in mail["text"] and target not in mail["text"], "the new address appears masked"
    assert " UTC" in mail["text"] and "not you" in mail["text"].lower()
    assert "<html>" in mail["html"] and "{{" not in mail["html"] and "{{" not in mail["text"]
    assert old not in caplog.text and target not in caplog.text, "addresses are masked in the log"


async def test_the_plain_path_without_an_email_change_needs_no_password(db, account, sent):
    async with fresh_client() as c:
        r = await c.put("/api/user/profile", json={"email": account.email.upper()}, headers=auth_headers(account))
    assert r.status_code == 200 and r.json()["email"] == account.email
    assert sent == []


async def test_the_new_address_must_be_free(db, account, sent):
    other = User(email=f"taken-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.add(other)
    await db.commit()
    try:
        async with fresh_client() as c:
            r = await c.put("/api/user/profile", json={"email": other.email, "current_password": PW}, headers=auth_headers(account))
        assert r.status_code == 400 and "already in use" in r.text
        assert sent == []
    finally:
        await db.delete(other)
        await db.commit()


async def test_a_locked_account_is_refused_with_429_exactly_like_login(db, account, sent):
    """The lockout the wrong-password counter feeds is honoured here too:
    same status, same sentence, same Retry-After as /api/auth/login."""
    from api.services.redis_client import get_redis
    await get_redis().setex(f"login-lockout:{account.email}", 870, "1")
    try:
        async with fresh_client() as c:
            r = await c.put("/api/user/profile", json={"email": new_address(), "current_password": PW},
                            headers=auth_headers(account))
            login = await c.post("/api/auth/login", json={"email": account.email, "password": PW})
    finally:
        await get_redis().delete(f"login-lockout:{account.email}")
    assert r.status_code == login.status_code == 429, (r.text, login.text)
    assert r.json() == login.json() and r.json()["detail"].startswith("Account locked after repeated failed logins.")
    assert abs(int(r.headers["Retry-After"]) - int(login.headers["Retry-After"])) <= 1
    await db.refresh(account)
    assert account.email.startswith("prof-") and sent == []


async def test_profile_updates_are_limited_to_five_a_minute(db, account, sent):
    from api.services.rate_limit import limiter
    limiter.reset()
    limiter.enabled = True    # off under tests by default, as in test_rate_limit.py
    try:
        async with fresh_client() as c:
            codes = [(await c.put("/api/user/profile", json={"email": account.email}, headers=auth_headers(account))).status_code
                     for _ in range(6)]
    finally:
        limiter.enabled = False
        limiter.reset()
    assert codes == [200] * 5 + [429]


async def test_the_profile_reports_two_factor_state(db, account, monkeypatch):
    async with fresh_client() as c:
        assert (await c.get("/api/user/profile", headers=auth_headers(account))).json()["mfa_enabled"] is False
    await enable_two_factor(db, account, monkeypatch)
    async with fresh_client() as c:
        body = (await c.get("/api/user/profile", headers=auth_headers(account))).json()
    assert body["mfa_enabled"] is True and body["guardian_enabled"] is False
