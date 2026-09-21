"""Focused tests for branches the main suites don't reach."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jose import jwt

import api.routes.kyc as kyc_mod
from api.auth import create_access_token
from api.config import settings
from api.models import User
from api.services.sumsub_client import SumsubError
from tests.api.conftest import auth_headers


# ---------------------------------------------------------------- app lifespan

async def test_lifespan_and_service_endpoints(monkeypatch):
    from asgi_lifespan import LifespanManager
    from api.main import app
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    async with LifespanManager(app):
        assert app.state.autobuy_scheduler.running
        assert app.state.autobuy_scheduler.get_job("execute_purchases") is not None
        assert app.state.autobuy_scheduler.get_job("weekly_digest") is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            h = (await c.get("/health")).json()
            assert h["status"] == "healthy" and h["schedulers"]["autobuy"]["running"] is True
            root = (await c.get("/")).json()
            assert root["docs"] == "/docs"
    assert app.state.autobuy_scheduler is None, "shutdown must stop the scheduler"


async def test_init_db_skipped_in_production(monkeypatch):
    import api.database as dbm
    monkeypatch.setenv("API_ENV", "production")
    called = []
    class Boom:
        def begin(self):
            called.append(1)
            raise AssertionError("must not touch the engine in production")
    monkeypatch.setattr(dbm, "engine", Boom())
    await dbm.init_db()
    assert not called


# ---------------------------------------------------------------- auth refresh

async def test_refresh_happy_path_rotates_tokens(client, user):
    from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE, _create_refresh_token
    refresh = _create_refresh_token(str(user.id))
    async with client as c:
        r = await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: refresh, CSRF_COOKIE: "c1"}, headers={CSRF_HEADER: "c1"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["csrf_token"] and "refresh_token" not in body
    assert r.cookies.get(REFRESH_COOKIE) not in (None, refresh), "rotated"


async def test_refresh_for_deleted_user_401(client):
    from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE, _create_refresh_token
    refresh = _create_refresh_token(str(uuid.uuid4()))
    async with client as c:
        assert (await c.post("/api/auth/refresh", cookies={REFRESH_COOKIE: refresh, CSRF_COOKIE: "c1"},
                             headers={CSRF_HEADER: "c1"})).status_code == 401


async def test_token_without_sub_and_bad_uuid_rejected(client, user):
    def forged(claims):
        claims.setdefault("exp", datetime.now(timezone.utc) + timedelta(minutes=5))
        return jwt.encode(claims, settings.secret_key, algorithm=settings.algorithm)
    async with client as c:
        for token in (forged({}), forged({"sub": "not-a-uuid"})):
            r = await c.get("/api/user/profile",
                            headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401


# ---------------------------------------------------------------- kyc routes

class FakeSumsub:
    configured = True
    def __init__(self, *a, **k): pass
    async def create_applicant(self, external_id, email): return "applicant-xyz"
    async def get_access_token(self, external_id): return "sdk-token-abc"
    async def get_applicant_status(self, applicant_id): return "verified"


class UnconfiguredSumsub(FakeSumsub):
    configured = False


class BrokenSumsub(FakeSumsub):
    async def create_applicant(self, *a): raise SumsubError("boom")
    async def get_applicant_status(self, *a): raise SumsubError("boom")


async def test_kyc_initiate_creates_applicant(client, db, user, monkeypatch):
    monkeypatch.setattr(kyc_mod, "SumsubClient", FakeSumsub)
    user.kyc_status = "pending"
    await db.commit()
    async with client as c:
        r = await c.post("/api/kyc/initiate", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json() == {"access_token": "sdk-token-abc", "applicant_id": "applicant-xyz"}
    await db.refresh(user)
    assert user.kyc_sumsub_id == "applicant-xyz"


async def test_kyc_initiate_when_already_verified_400(client, user, monkeypatch):
    monkeypatch.setattr(kyc_mod, "SumsubClient", FakeSumsub)
    async with client as c:  # conftest user is verified
        assert (await c.post("/api/kyc/initiate",
                             headers=auth_headers(user))).status_code == 400


async def test_kyc_unconfigured_503(client, db, user, monkeypatch):
    monkeypatch.setattr(kyc_mod, "SumsubClient", UnconfiguredSumsub)
    user.kyc_status = "pending"
    user.kyc_sumsub_id = "applicant-1"
    await db.commit()
    async with client as c:
        assert (await c.post("/api/kyc/initiate", headers=auth_headers(user))).status_code == 503
    # status degrades gracefully: the stored status is returned instead of a 503 (works on localhost)
    import api.integrations.sumsub as sumsub_mod
    monkeypatch.setattr(sumsub_mod, "is_configured", lambda: False)
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/kyc/status", headers=auth_headers(user))
    assert r.status_code == 200 and r.json()["kyc_status"] == "pending" and r.json()["synced"] is False


async def test_kyc_status_syncs_from_provider(client, db, user, monkeypatch):
    import api.integrations.sumsub as sumsub_mod

    async def fake_status(applicant_id):
        return {"applicant_id": applicant_id, "review_status": "completed", "review_answer": "GREEN",
                "reject_labels": [], "kyc_status": "verified",
                "raw": {"reviewStatus": "completed", "reviewResult": {"reviewAnswer": "GREEN"}}}
    monkeypatch.setattr(sumsub_mod, "is_configured", lambda: True)
    monkeypatch.setattr(sumsub_mod, "get_applicant_status", fake_status)
    user.kyc_status = "pending"
    user.kyc_sumsub_id = "applicant-1"
    await db.commit()
    async with client as c:
        r = await c.get("/api/kyc/status", headers=auth_headers(user))
    assert r.json()["kyc_status"] == "verified" and r.json()["synced"] is True
    await db.refresh(user)
    assert user.kyc_status == "verified"


async def test_kyc_status_without_applicant_returns_stored(client, db, user):
    user.kyc_status = "pending"
    user.kyc_sumsub_id = None
    await db.commit()
    async with client as c:
        body = (await c.get("/api/kyc/status", headers=auth_headers(user))).json()
    assert body["kyc_status"] == "pending" and body["applicant_id"] is None and body["synced"] is False


async def test_kyc_provider_outage(client, db, user, monkeypatch):
    import api.integrations.sumsub as sumsub_mod
    from api.services.sumsub_client import SumsubError

    async def down(applicant_id):
        raise SumsubError("Sumsub unreachable: timeout")
    monkeypatch.setattr(sumsub_mod, "is_configured", lambda: True)
    monkeypatch.setattr(sumsub_mod, "get_applicant_status", down)
    monkeypatch.setattr(kyc_mod, "SumsubClient", BrokenSumsub)
    user.kyc_status = "pending"
    user.kyc_sumsub_id = "applicant-1"
    await db.commit()
    async with client as c:
        r = await c.get("/api/kyc/status", headers=auth_headers(user))
        assert r.status_code == 200 and r.json()["kyc_status"] == "pending" and r.json()["synced"] is False  # stale, not error
    from api.main import app
    user.kyc_sumsub_id = None
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/api/kyc/initiate", headers=auth_headers(user))).status_code == 502


async def test_kyc_webhook_unknown_applicant_ok(client, monkeypatch):
    import hashlib, hmac as hmac_mod, json
    monkeypatch.setattr(settings, "sumsub_webhook_secret", "whsec")
    body = json.dumps({"applicantId": "who-dis", "reviewStatus": "completed",
                       "reviewResult": {"reviewAnswer": "GREEN"}}).encode()
    sig = hmac_mod.new(b"whsec", body, hashlib.sha256).hexdigest()
    async with client as c:
        r = await c.post("/api/kyc/webhook", content=body,
                         headers={"x-payload-digest": sig, "content-type": "application/json"})
    assert r.status_code == 200 and r.json() == {"received": True}


# ---------------------------------------------------------------- exchange list/delete

async def test_list_and_delete_own_key(client, db, user, binance_key):
    H = auth_headers(user)
    async with client as c:
        keys = (await c.get("/api/exchange/keys", headers=H)).json()
        assert len(keys) == 1 and keys[0]["api_key_masked"].count(".") == 3
        assert (await c.delete(f"/api/exchange/keys/{binance_key.id}", headers=H)).status_code == 204
        assert (await c.get("/api/exchange/keys", headers=H)).json() == []


# ---------------------------------------------------------------- misc branches

def test_binance_account_info_and_balance(monkeypatch):
    import api.services.binance_client as bc_mod
    class FakeLegacy:
        def __init__(self, **kw): pass
        def _request(self, method, path, params=None, auth=False):
            return {"canTrade": True, "canWithdraw": False, "permissions": ["SPOT"]}
        def get_spot_available(self, coin): return 3.14
    monkeypatch.setattr(bc_mod, "_LegacyClient", lambda **kw: FakeLegacy())
    c = bc_mod.BinanceClient("k", "s")
    assert c.get_account_info() == {"can_trade": True, "can_withdraw": False, "permissions": ["SPOT"], "permissions_known": True}
    assert c.get_account_balance("BTC") == 3.14


async def test_email_send_real_path(monkeypatch):
    """The fire-and-forget wrapper delegates to api.integrations.resend (the only Resend client):
    True on a 2xx from Resend, False (never an exception) on a rejection."""
    from api.integrations import resend
    import api.services.email_service as em
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Test <noreply@example.com>")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"id": "email-1"})
    monkeypatch.setattr(resend, "_transport", httpx.MockTransport(handler))
    svc = em.EmailService(api_key="rk_test")
    assert await svc._send("a@b.co", "Hi", "<p>x</p>", "x") is True
    assert sent and b"a@b.co" in sent[0].content and b"noreply@example.com" in sent[0].content

    monkeypatch.setattr(resend, "_transport", httpx.MockTransport(
        lambda request: httpx.Response(422, json={"message": "invalid from"})))
    assert await svc._send("a@b.co", "Hi", "<p>x</p>", "x") is False
