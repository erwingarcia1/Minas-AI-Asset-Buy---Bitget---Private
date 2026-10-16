"""Two-factor management (/api/user/2fa), the admin requirement and the admin reset."""
import uuid

import httpx
import pytest
from sqlalchemy import select

from api.auth import hash_password
from api.config import settings
from api.models import User
from api.services import twofactor
from api.services.totp import totp_at

PW = "Routes-Passw0rd!1"
STEP = 30


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(twofactor, "_clock", lambda: c.now)
    return c


@pytest.fixture
def notices(monkeypatch):
    sent = []

    async def fake(email, kind, **ctx):
        sent.append((email, kind))

    import api.routes.twofactor as tf_routes
    monkeypatch.setattr(tf_routes, "send_two_factor_notice", fake)
    return sent


async def make_user(db, role="user"):
    u = User(email=f"tf-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(PW), kyc_status="verified", role=role)
    db.add(u)
    await db.commit()
    from api.services.redis_client import get_redis
    await get_redis().delete(f"login-lockout:{u.email}", f"login-fails:{u.email}")
    return u


async def drop(db, user):
    fresh = (await db.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
async def account(db):
    u = await make_user(db)
    yield u
    await drop(db, u)


@pytest.fixture
async def admin(db):
    u = await make_user(db, role="admin")
    yield u
    await drop(db, u)


def headers(user):
    from api.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def enrol_via_api(c, user, clock):
    enrol = await c.post("/api/user/2fa/enrol", json={"password": PW}, headers=headers(user))
    assert enrol.status_code == 200, enrol.text
    secret = enrol.json()["secret"]
    confirm = await c.post("/api/user/2fa/confirm", json={"code": totp_at(secret, clock.now)}, headers=headers(user))
    assert confirm.status_code == 200, confirm.text
    clock.advance(STEP)
    return secret, confirm.json()["recovery_codes"]


async def test_status_starts_off(account):
    async with client() as c:
        r = await c.get("/api/user/2fa", headers=headers(account))
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "enrolled_at": None, "recovery_codes_remaining": 0, "pending_enrolment": False}


async def test_enrolment_needs_the_password_and_answers_a_secret_once(account, clock):
    async with client() as c:
        wrong = await c.post("/api/user/2fa/enrol", json={"password": "nope"}, headers=headers(account))
        assert wrong.status_code == 400, "400, not 401: the session is valid, the password is wrong, and a 401 would make the web client refresh and replay it"
        r = await c.post("/api/user/2fa/enrol", json={"password": PW}, headers=headers(account))
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"secret", "otpauth_uri"}
        assert body["otpauth_uri"].startswith("otpauth://totp/") and settings.app_name.split()[0] in body["otpauth_uri"]
        assert account.email.replace("@", "%40") in body["otpauth_uri"]
        status = (await c.get("/api/user/2fa", headers=headers(account))).json()
        assert status["pending_enrolment"] is True and status["enabled"] is False


async def test_confirming_turns_it_on_issues_codes_and_notifies(account, clock, notices):
    async with client() as c:
        enrol = await c.post("/api/user/2fa/enrol", json={"password": PW}, headers=headers(account))
        secret = enrol.json()["secret"]
        bad = await c.post("/api/user/2fa/confirm", json={"code": "000000"}, headers=headers(account))
        assert bad.status_code == 400 and "not accepted" in bad.json()["detail"]
        good = await c.post("/api/user/2fa/confirm", json={"code": totp_at(secret, clock.now)}, headers=headers(account))
        assert good.status_code == 200 and len(good.json()["recovery_codes"]) == 10
        status = (await c.get("/api/user/2fa", headers=headers(account))).json()
        assert status["enabled"] is True and status["recovery_codes_remaining"] == 10 and status["enrolled_at"]
        again = await c.post("/api/user/2fa/enrol", json={"password": PW}, headers=headers(account))
        assert again.status_code == 409
    assert notices == [(account.email, "enabled")]


async def test_regenerating_recovery_codes_needs_a_current_code(account, clock, notices):
    async with client() as c:
        secret, old = await enrol_via_api(c, account, clock)
        bad = await c.post("/api/user/2fa/recovery-codes", json={"code": old[0]}, headers=headers(account))
        assert bad.status_code == 400, "a recovery code cannot mint recovery codes"
        good = await c.post("/api/user/2fa/recovery-codes", json={"code": totp_at(secret, clock.now)}, headers=headers(account))
        assert good.status_code == 200
        new = good.json()["recovery_codes"]
        assert len(new) == 10 and not set(new) & set(old)
        assert (await c.get("/api/user/2fa", headers=headers(account))).json()["recovery_codes_remaining"] == 10
    assert notices[-1] == (account.email, "codes_regenerated")


async def test_disabling_needs_password_and_a_factor_and_notifies(account, clock, notices):
    async with client() as c:
        secret, codes = await enrol_via_api(c, account, clock)
        H = headers(account)
        assert (await c.post("/api/user/2fa/disable", json={"password": "nope", "code": totp_at(secret, clock.now)}, headers=H)).status_code == 400
        assert (await c.post("/api/user/2fa/disable", json={"password": PW, "code": "000000"}, headers=H)).status_code == 400
        assert (await c.post("/api/user/2fa/disable", json={"password": PW}, headers=H)).status_code == 422
        assert (await c.get("/api/user/2fa", headers=H)).json()["enabled"] is True
        ok = await c.post("/api/user/2fa/disable", json={"password": PW, "recovery_code": codes[0]}, headers=H)
        assert ok.status_code == 204
        assert (await c.get("/api/user/2fa", headers=H)).json() == {"enabled": False, "enrolled_at": None,
                                                                     "recovery_codes_remaining": 0, "pending_enrolment": False}
        assert (await c.post("/api/user/2fa/disable", json={"password": PW, "recovery_code": codes[1]}, headers=H)).status_code == 400
    assert notices[-1] == (account.email, "disabled")


async def test_admin_routes_require_two_factor_when_the_setting_is_on(admin, clock, monkeypatch):
    monkeypatch.setattr(settings, "admin_mfa_required", True)
    async with client() as c:
        H = headers(admin)
        blocked = await c.get("/api/admin/stats", headers=H)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["error"] == "mfa_required_for_admin"
        assert (await c.get("/api/user/2fa", headers=H)).status_code == 200, "the admin can still reach the enrolment routes"
        await enrol_via_api(c, admin, clock)
        assert (await c.get("/api/admin/stats", headers=H)).status_code == 200
        off = await c.post("/api/user/2fa/disable", json={"password": PW, "code": "000000"}, headers=H)
        assert off.status_code == 403, "an admin cannot turn it off while the setting is on (checked before the code)"


async def test_admin_routes_do_not_require_two_factor_by_default(admin):
    assert settings.admin_mfa_required is False
    async with client() as c:
        assert (await c.get("/api/admin/stats", headers=headers(admin))).status_code == 200


async def test_admin_can_reset_a_users_two_factor(db, admin, account, clock, notices):
    async with client() as c:
        await enrol_via_api(c, account, clock)
        r = await c.post(f"/api/admin/users/{account.id}/2fa/reset", headers=headers(admin))
        assert r.status_code == 204
        assert (await c.get("/api/user/2fa", headers=headers(account))).json()["enabled"] is False
        again = await c.post(f"/api/admin/users/{account.id}/2fa/reset", headers=headers(admin))
        assert again.status_code == 204, "idempotent"
        missing = await c.post(f"/api/admin/users/{uuid.uuid4()}/2fa/reset", headers=headers(admin))
        assert missing.status_code == 404
    assert (account.email, "reset_by_admin") in notices and notices.count((account.email, "reset_by_admin")) == 1


async def test_admin_user_list_shows_who_has_two_factor(db, admin, account, clock):
    async with client() as c:
        await enrol_via_api(c, account, clock)
        rows = (await c.get("/api/admin/users", params={"q": account.email}, headers=headers(admin))).json()["items"]
        assert [r["totp_enabled"] for r in rows if r["email"] == account.email] == [True]
        rows = (await c.get("/api/admin/users", params={"q": admin.email}, headers=headers(admin))).json()["items"]
        assert [r["totp_enabled"] for r in rows if r["email"] == admin.email] == [False]


async def test_no_secret_or_code_reaches_the_log(account, clock, caplog):
    with caplog.at_level("DEBUG"):
        async with client() as c:
            secret, codes = await enrol_via_api(c, account, clock)
    for forbidden in (secret, *codes):
        assert forbidden not in caplog.text


async def test_an_admin_cannot_reset_their_own_two_factor_through_the_admin_route(db, admin, clock, notices):
    """A stolen admin session must not be able to strip its owner's second factor."""
    async with client() as c:
        await enrol_via_api(c, admin, clock)
        r = await c.post(f"/api/admin/users/{admin.id}/2fa/reset", headers=headers(admin))
        assert r.status_code == 403 and "own account" in r.json()["detail"]
        assert (await c.get("/api/user/2fa", headers=headers(admin))).json()["enabled"] is True
    assert (admin.email, "reset_by_admin") not in notices


async def test_an_admin_with_two_factor_on_proves_it_before_resetting_someone_else(db, admin, account, clock, notices):
    async with client() as c:
        admin_secret, _ = await enrol_via_api(c, admin, clock)
        await enrol_via_api(c, account, clock)
        no_code = await c.post(f"/api/admin/users/{account.id}/2fa/reset", headers=headers(admin))
        assert no_code.status_code == 400 and "authenticator" in no_code.json()["detail"]
        wrong = await c.post(f"/api/admin/users/{account.id}/2fa/reset", json={"code": "000000"}, headers=headers(admin))
        assert wrong.status_code == 400
        assert (await c.get("/api/user/2fa", headers=headers(account))).json()["enabled"] is True, "nothing was removed"
        ok = await c.post(f"/api/admin/users/{account.id}/2fa/reset",
                          json={"code": totp_at(admin_secret, clock.now)}, headers=headers(admin))
        assert ok.status_code == 204, ok.text
        assert (await c.get("/api/user/2fa", headers=headers(account))).json()["enabled"] is False
    assert (account.email, "reset_by_admin") in notices


async def test_a_validation_error_never_echoes_the_submitted_password(account):
    """FastAPI's default 422 body carries the submitted input; for a password field that is the password."""
    async with client() as c:
        r = await c.post("/api/user/2fa/disable",
                         json={"password": "Echo-Me-Not!9", "code": "000000", "recovery_code": "AAAAA-BBBBB"},
                         headers=headers(account))
    assert r.status_code == 422
    assert "Echo-Me-Not!9" not in r.text
    assert r.json()["detail"][0]["msg"]
