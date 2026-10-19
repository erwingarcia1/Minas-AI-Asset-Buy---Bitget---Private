"""Accounts provisioned without a password (v0.9.2): members migrated from
Minas AI Guardian and customers an admin onboards.

`provision_user` creates the user once, with an unusable random password and
`password_reset_required` set. Until the customer sets a password through the
seven-day link, a sign-in is refused exactly like a wrong password (same body,
same headers, same lockout counter) and the activation email is re-sent at
most once an hour. The ordinary reset flow activates the account.
"""
import uuid

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

from api.auth import hash_password
from api.config import settings
from api.models import User
from api.routes.auth import make_mfa_token
from api.services import guardian_identity, twofactor
from api.services.totp import totp_at

PW = "Ordinary-Passw0rd!1"
NEW = "Chosen-Passw0rd!2"
GUESS = "Guess-Passw0rd!3"


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def sent(monkeypatch):
    """Security emails are queued to the outbox; capture them there."""
    box = []

    async def fake_enqueue(to, subject, html, text, template="generic", *, category="confirmation", user_id=None, now=None):
        box.append({"to": to, "subject": subject, "html": html, "text": text, "template": template, "category": category})
        return True
    from api.services import email_outbox
    monkeypatch.setattr(email_outbox, "enqueue", fake_enqueue)
    return box


async def _cleanup(db, email: str) -> None:
    from api.services.redis_client import get_redis
    user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
    if user is not None:
        await get_redis().delete(guardian_identity.ACTIVATION_SENT_PREFIX + str(user.id),
                                 f"login-fails:{user.email}", f"login-lockout:{user.email}")
        await db.delete(user)
        await db.commit()


@pytest.fixture
async def provisioned(db):
    email = f"member-{uuid.uuid4().hex[:8]}@example.com"
    user, created = await guardian_identity.provision_user(db, email)
    assert created is True
    yield user
    await _cleanup(db, email)


@pytest.fixture
async def ordinary(db):
    u = User(email=f"ordinary-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    await _cleanup(db, u.email)


def link_token(text: str) -> str:
    return text.split("token=", 1)[1].split()[0]


# --------------------------------------------------------------------------- provisioning

async def test_provision_user_creates_once_and_then_returns_the_existing_user(db):
    email = f"Member-{uuid.uuid4().hex[:8]}@Example.com"
    try:
        user, created = await guardian_identity.provision_user(db, email)
        assert created is True
        assert user.email == email.lower() and user.password_reset_required is True
        assert user.kyc_status == "pending" and user.role == "user" and user.suspended is False
        assert user.password_hash.startswith("$argon2"), "an unusable but well-formed hash"
        again, created_again = await guardian_identity.provision_user(db, email.upper())
        assert created_again is False and again.id == user.id
        rows = (await db.execute(select(User).where(User.email == email.lower()))).scalars().all()
        assert len(rows) == 1
    finally:
        await _cleanup(db, email)


async def test_provision_user_can_create_an_admin(db):
    email = f"operator-{uuid.uuid4().hex[:8]}@example.com"
    try:
        user, created = await guardian_identity.provision_user(db, email, role="admin")
        assert created is True and user.role == "admin" and user.password_reset_required is True
    finally:
        await _cleanup(db, email)


# --------------------------------------------------------------------------- sign-in refusal

async def test_a_provisioned_user_is_refused_exactly_like_a_wrong_password(provisioned, ordinary, sent):
    async with fresh_client() as c:
        wrong = await c.post("/api/auth/login", json={"email": ordinary.email, "password": GUESS})
        refused = await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})
        refused_again = await c.post("/api/auth/login", json={"email": provisioned.email, "password": "Another-Guess!4"})
    assert wrong.status_code == 401
    for r in (refused, refused_again):
        assert r.status_code == wrong.status_code
        assert r.json() == wrong.json() == {"detail": "Invalid email or password"}
        strip = lambda h: {k: v for k, v in h.items() if k != "x-request-id"}   # the tracing id differs per request
        assert strip(r.headers) == strip(wrong.headers), "same headers, same length: nothing tells the two apart"
        assert r.headers.get_list("set-cookie") == []


async def test_refusals_count_towards_the_same_lockout(provisioned, sent):
    async with fresh_client() as c:
        for _ in range(5):
            assert (await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})).status_code == 401
        locked = await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})
    assert locked.status_code == 429 and "Retry-After" in locked.headers


async def test_the_activation_email_is_queued_once_and_not_again_within_the_hour(provisioned, sent, monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://app.autobuy.test")
    from api.services.redis_client import get_redis
    async with fresh_client() as c:
        for _ in range(3):
            assert (await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})).status_code == 401
    assert len(sent) == 1, "three refused attempts, one email"
    mail = sent[0]
    assert mail["to"] == provisioned.email and mail["template"] == "guardian_activate" and mail["category"] == "security"
    assert mail["subject"] == f"Set your {settings.app_name} password"
    assert "https://app.autobuy.test/reset-password?token=" in mail["text"]
    assert "Minas AI Guardian" in mail["text"] and "7 days" in mail["text"]
    assert "<html>" in mail["html"] and "{{" not in mail["html"] and "{{" not in mail["text"]
    decoded = jwt.decode(link_token(mail["text"]), settings.secret_key, algorithms=[settings.algorithm])
    assert decoded["type"] == "password_reset" and decoded["sub"] == str(provisioned.id)
    assert decoded["exp"] - decoded["iat"] == 7 * 24 * 3600, "a seven-day link, not the one-hour reset"
    ttl = await get_redis().ttl(guardian_identity.ACTIVATION_SENT_PREFIX + str(provisioned.id))
    assert 0 < ttl <= 3600


async def test_the_throttle_key_alone_decides(provisioned, sent):
    from api.services.redis_client import get_redis
    key = guardian_identity.ACTIVATION_SENT_PREFIX + str(provisioned.id)
    await get_redis().set(key, "1", ex=3600)
    assert await guardian_identity.maybe_send_activation(provisioned) is False
    assert sent == []
    await get_redis().delete(key)
    assert await guardian_identity.maybe_send_activation(provisioned) is True
    assert len(sent) == 1


async def test_redis_down_skips_the_activation_email_without_raising(provisioned, sent, monkeypatch, caplog):
    class Down:
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(guardian_identity, "_redis", lambda: Down())
    with caplog.at_level("INFO"):
        assert await guardian_identity.maybe_send_activation(provisioned) is False
    assert sent == []
    assert "ConnectionError" in caplog.text and provisioned.email not in caplog.text, "logged, masked, not raised"


# --------------------------------------------------------------------------- activation

async def test_the_seven_day_link_sets_the_password_and_login_then_works(db, provisioned, sent):
    link = guardian_identity.activation_link(provisioned)
    token = link_token(link)
    async with fresh_client() as c:
        assert (await c.post("/api/auth/reset-password/validate", json={"token": token})).json() == {"valid": True}
        r = await c.post("/api/auth/reset-password", json={"token": token, "new_password": NEW})
        assert r.status_code == 204, r.text
        await db.refresh(provisioned)
        assert provisioned.password_reset_required is False
        ok = await c.post("/api/auth/login", json={"email": provisioned.email, "password": NEW})
        assert ok.status_code == 200, ok.text
        assert set(ok.json()) == {"access_token", "token_type", "expires_in", "csrf_token"}
        assert (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {ok.json()['access_token']}"})).status_code == 200
        # the link is single-use, and a wrong password is now just a wrong password
        assert (await c.post("/api/auth/reset-password", json={"token": token, "new_password": "Third-Passw0rd!5"})).status_code == 400
        assert (await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})).status_code == 401
    assert [m["template"] for m in sent] == ["password_changed"], "the reset's own notice; no activation mail once a password exists"


async def test_login_mfa_refuses_a_pending_token_of_a_provisioned_user(db, provisioned, sent, monkeypatch):
    """A two-factor enrolment carried over without a password: even a valid
    pending token plus the right code does not finish the sign-in."""
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(twofactor, "_clock", lambda: clock["now"])
    secret, _ = await twofactor.begin_enrolment(db, provisioned)
    assert await twofactor.confirm_enrolment(db, provisioned, totp_at(secret, clock["now"])) is not None
    clock["now"] += 30
    token = make_mfa_token(provisioned)
    async with fresh_client() as c:
        r = await c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": totp_at(secret, clock["now"])})
    assert r.status_code == 401 and r.headers.get_list("set-cookie") == []
    assert r.json() == {"detail": "Invalid or expired sign-in. Start again from the login page."}
    assert [m["template"] for m in sent] == ["guardian_activate"]


async def test_nothing_secret_reaches_the_log(provisioned, sent, caplog):
    with caplog.at_level("DEBUG"):
        async with fresh_client() as c:
            await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})
            await c.post("/api/auth/login", json={"email": provisioned.email, "password": GUESS})
    assert len(sent) == 1
    text = caplog.text
    for forbidden in (link_token(sent[0]["text"]), GUESS, provisioned.password_hash, provisioned.email):
        assert forbidden not in text
