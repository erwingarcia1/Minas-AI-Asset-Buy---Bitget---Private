"""Sumsub KYC + Resend email integrations: modules and routes.

Every outbound HTTP call is served by an httpx.MockTransport injected into
the integration modules, so nothing here touches the network. Route tests
run through the real ASGI app against the local database and clean up the
users they create.

    pytest tests/test_integrations.py -v
"""
import hashlib
import hmac
import json
import os
import uuid

os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
os.environ.setdefault("AUTOBUY_DB_POOL", "null")

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from api.auth import create_access_token
from api.config import settings
from api.database import AsyncSessionLocal
from api.integrations import resend, sumsub
from api.integrations.resend import ResendError
from api.integrations.sumsub import SumsubError
from api.main import app
from api.models import User
from api.routes.email import make_verification_token, verification_link
from api.routes.user import get_current_user  # noqa: F401  (guard under test)

APP_TOKEN, SECRET_KEY, WEBHOOK_SECRET = "sbx:test-app-token", "test-secret-key", "test-webhook-secret"
API_KEY = "re_test_123"


# --------------------------------------------------------------------------- helpers / fixtures

class Recorder:
    """MockTransport handler that records requests and replies per (method, path)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple, httpx.Response] = {}
        self.default = httpx.Response(404, json={"description": "not found"})

    def on(self, method: str, path: str, status: int = 200, body=None):
        self.responses[(method.upper(), path)] = httpx.Response(status, json=body if body is not None else {})
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.get((request.method, request.url.path), self.default)

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def last_json(self):
        return json.loads(self.requests[-1].content)


def sign(body: bytes, secret: str = WEBHOOK_SECRET, algo=hashlib.sha256) -> str:
    return hmac.new(secret.encode(), body, algo).hexdigest()


@pytest.fixture
def sumsub_env(monkeypatch):
    monkeypatch.setenv("SUMSUB_APP_TOKEN", APP_TOKEN)
    monkeypatch.setenv("SUMSUB_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("SUMSUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("SUMSUB_LEVEL_NAME", "basic-kyc-level")
    rec = Recorder()
    monkeypatch.setattr(sumsub, "_transport", rec.transport)
    return rec


@pytest.fixture
def sumsub_unconfigured(monkeypatch):
    for k in ("SUMSUB_APP_TOKEN", "SUMSUB_SECRET_KEY", "SUMSUB_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(settings, "sumsub_app_token", "")
    monkeypatch.setattr(settings, "sumsub_secret_key", "")
    monkeypatch.setattr(settings, "sumsub_webhook_secret", "")


@pytest.fixture
def resend_env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", API_KEY)
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Test Sender <noreply@example.test>")
    monkeypatch.delenv("RESEND_REPLY_TO", raising=False)
    monkeypatch.delenv("SUPPORT_EMAIL", raising=False)
    monkeypatch.setattr(settings, "resend_reply_to", "")
    monkeypatch.setattr(settings, "support_email", "")
    rec = Recorder().on("POST", "/emails", 200, {"id": "email-abc123"})
    monkeypatch.setattr(resend, "_transport", rec.transport)
    return rec


@pytest.fixture
def resend_unconfigured(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(settings, "resend_api_key", "")


@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def user(db):
    u = User(email=f"integ-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", kyc_status="pending")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest_asyncio.fixture
async def admin(db):
    u = User(email=f"integ-admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
             kyc_status="verified", role="admin")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


def auth(u: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(u.id)).access_token}"}


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- sumsub module

async def test_create_applicant_signs_request_and_returns_id(sumsub_env):
    sumsub_env.on("POST", "/resources/applicants", 201, {"id": "app-123", "externalUserId": "u-1"})
    applicant_id = await sumsub.create_applicant("Jane@Example.com", " Jane ", "Doe", external_user_id="u-1")
    assert applicant_id == "app-123"
    req = sumsub_env.requests[-1]
    assert req.method == "POST" and req.url.path == "/resources/applicants"
    assert req.url.params["levelName"] == "basic-kyc-level"
    assert req.headers["x-app-token"] == APP_TOKEN
    assert req.headers["x-app-access-ts"].isdigit() and len(req.headers["x-app-access-sig"]) == 64
    # signature = HMAC-SHA256(secret, ts + METHOD + path?query + body)
    body = req.content.decode()
    expected = hmac.new(SECRET_KEY.encode(),
                        f"{req.headers['x-app-access-ts']}POST/resources/applicants?levelName=basic-kyc-level{body}".encode(),
                        hashlib.sha256).hexdigest()
    assert req.headers["x-app-access-sig"] == expected
    assert sumsub_env.last_json() == {"externalUserId": "u-1", "email": "jane@example.com",
                                      "fixedInfo": {"firstName": "Jane", "lastName": "Doe"}}


async def test_create_applicant_defaults_external_id_to_email_and_validates(sumsub_env):
    sumsub_env.on("POST", "/resources/applicants", 201, {"id": "app-9"})
    await sumsub.create_applicant("a@b.co", "A", "B")
    assert sumsub_env.last_json()["externalUserId"] == "a@b.co"
    with pytest.raises(SumsubError):
        await sumsub.create_applicant("a@b.co", "", "B")
    assert len(sumsub_env.requests) == 1, "validation happens before any request"


async def test_create_applicant_error_paths(sumsub_env):
    sumsub_env.on("POST", "/resources/applicants", 401, {"description": "Unauthorized"})
    with pytest.raises(SumsubError, match="401"):
        await sumsub.create_applicant("a@b.co", "A", "B")
    sumsub_env.on("POST", "/resources/applicants", 201, {"weird": True})
    with pytest.raises(SumsubError, match="applicant id"):
        await sumsub.create_applicant("a@b.co", "A", "B")


async def test_create_applicant_requires_credentials(sumsub_unconfigured):
    assert not sumsub.is_configured()
    with pytest.raises(SumsubError, match="not configured"):
        await sumsub.create_applicant("a@b.co", "A", "B")


async def test_create_applicant_transport_failure(sumsub_env, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("dns failure", request=request)
    monkeypatch.setattr(sumsub, "_transport", httpx.MockTransport(boom))
    with pytest.raises(SumsubError, match="unreachable"):
        await sumsub.create_applicant("a@b.co", "A", "B")


@pytest.mark.parametrize("review_status, answer, labels, expected", [
    ("completed", "GREEN", [], "verified"),
    ("completed", "RED", ["UNSATISFACTORY_PHOTOS"], "rejected"),
    ("pending", None, [], "pending"),
    ("init", None, [], "pending"),
])
async def test_get_applicant_status_maps_review(sumsub_env, review_status, answer, labels, expected):
    raw = {"reviewStatus": review_status,
           "reviewResult": {"reviewAnswer": answer, "rejectLabels": labels} if answer else None}
    sumsub_env.on("GET", "/resources/applicants/app-1/status", 200, raw)
    status = await sumsub.get_applicant_status("app-1")
    assert status["applicant_id"] == "app-1" and status["kyc_status"] == expected
    assert status["review_status"] == review_status and status["review_answer"] == answer
    assert status["reject_labels"] == labels and status["raw"] == raw
    assert sumsub_env.requests[-1].method == "GET"


async def test_get_applicant_status_errors(sumsub_env):
    sumsub_env.on("GET", "/resources/applicants/missing/status", 404, {"description": "no such applicant"})
    with pytest.raises(SumsubError, match="404"):
        await sumsub.get_applicant_status("missing")
    with pytest.raises(SumsubError):
        await sumsub.get_applicant_status("")


def test_verify_webhook_signature(monkeypatch):
    monkeypatch.setenv("SUMSUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = b'{"applicantId":"app-1","type":"applicantReviewed"}'
    assert sumsub.verify_webhook_signature(sign(body), body)
    assert sumsub.verify_webhook_signature(sign(body).upper(), body.decode()), "case-insensitive hex, str body ok"
    assert sumsub.verify_webhook_signature(sign(body, algo=hashlib.sha512), body, algorithm="HMAC_SHA512_HEX")
    assert sumsub.verify_webhook_signature(sign(body, algo=hashlib.sha1), body, algorithm="HMAC_SHA1_HEX")
    assert sumsub.verify_webhook_signature(sign(body, "other"), body, secret="other"), "explicit secret wins"
    assert not sumsub.verify_webhook_signature(sign(body), body + b" ")
    assert not sumsub.verify_webhook_signature("", body)
    assert not sumsub.verify_webhook_signature("deadbeef", body)
    assert not sumsub.verify_webhook_signature(sign(body), body, algorithm="MD5")
    monkeypatch.delenv("SUMSUB_WEBHOOK_SECRET")
    monkeypatch.setattr(settings, "sumsub_webhook_secret", "")
    assert not sumsub.verify_webhook_signature(sign(body), body), "no secret -> reject, never accept"


# --------------------------------------------------------------------------- resend module

async def test_send_email_success(resend_env):
    email_id = await resend.send_email("to@example.com", "Hi", "<p>hello</p>", text_content="hello")
    assert email_id == "email-abc123"
    req = resend_env.requests[-1]
    assert str(req.url) == resend.RESEND_URL and req.headers["authorization"] == f"Bearer {API_KEY}"
    assert resend_env.last_json() == {"from": "Test Sender <noreply@example.test>", "to": ["to@example.com"],
                                      "subject": "Hi", "html": "<p>hello</p>", "text": "hello"}


async def test_send_email_custom_from_and_env_override(resend_env, monkeypatch):
    await resend.send_email("to@example.com", "Hi", "<p>x</p>", from_email="ops@example.test", reply_to="support@x.co")
    assert resend_env.last_json()["from"] == "ops@example.test"
    assert resend_env.last_json()["reply_to"] == "support@x.co"
    monkeypatch.setenv("RESEND_FROM_EMAIL", "hello@example.test")
    await resend.send_email("to@example.com", "Hi", "<p>x</p>")
    assert resend_env.last_json()["from"] == "hello@example.test"


async def test_send_email_errors(resend_env, resend_unconfigured, monkeypatch):
    with pytest.raises(ResendError, match="not configured"):
        await resend.send_email("to@example.com", "Hi", "<p>x</p>")
    assert resend_env.requests == [], "no request without an API key"
    monkeypatch.setenv("RESEND_API_KEY", "your_resend_api_key_here")
    assert not resend.is_configured(), ".env.example placeholder counts as unconfigured"

    monkeypatch.setenv("RESEND_API_KEY", API_KEY)
    resend_env.on("POST", "/emails", 422, {"message": "Invalid `to` field"})
    with pytest.raises(ResendError) as ei:
        await resend.send_email("bad", "Hi", "<p>x</p>")
    assert ei.value.status == 422
    resend_env.on("POST", "/emails", 200, {})
    with pytest.raises(ResendError, match="email id"):
        await resend.send_email("to@example.com", "Hi", "<p>x</p>")
    with pytest.raises(ResendError, match="required"):
        await resend.send_email("to@example.com", "", "<p>x</p>")


async def test_send_email_transport_failure(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", API_KEY)
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Test Sender <noreply@example.test>")  # the suite never reads .env
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    monkeypatch.setattr(resend, "_transport", httpx.MockTransport(boom))
    with pytest.raises(ResendError, match="unreachable"):
        await resend.send_email("to@example.com", "Hi", "<p>x</p>")


# --------------------------------------------------------------------------- KYC routes

async def test_kyc_routes_require_jwt(client):
    body = {"email": "a@b.co", "first_name": "A", "last_name": "B"}
    assert (await client.post("/api/kyc/create-applicant", json=body)).status_code == 401
    assert (await client.get("/api/kyc/applicant/app-1/status")).status_code == 401
    assert (await client.post("/api/kyc/create-applicant", json=body,
                              headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_create_applicant_route_stores_id_and_is_idempotent(client, db, user, sumsub_env):
    sumsub_env.on("POST", "/resources/applicants", 201, {"id": "app-route-1"})
    body = {"email": user.email, "first_name": "Jane", "last_name": "Doe"}
    r = await client.post("/api/kyc/create-applicant", json=body, headers=auth(user))
    assert r.status_code == 200, r.text
    assert r.json() == {"applicant_id": "app-route-1", "kyc_status": "pending", "created": True}
    assert sumsub_env.last_json()["externalUserId"] == str(user.id)
    assert sumsub_env.last_json()["fixedInfo"] == {"firstName": "Jane", "lastName": "Doe"}
    await db.refresh(user)
    assert user.kyc_sumsub_id == "app-route-1"

    # second call reuses the stored applicant without calling Sumsub again
    r = await client.post("/api/kyc/create-applicant", json=body, headers=auth(user))
    assert r.status_code == 200 and r.json()["created"] is False
    assert len(sumsub_env.requests) == 1


async def test_create_applicant_route_guards(client, db, user, sumsub_env):
    # email must match the account
    r = await client.post("/api/kyc/create-applicant", headers=auth(user),
                          json={"email": "someone-else@example.com", "first_name": "A", "last_name": "B"})
    assert r.status_code == 403
    # validation
    r = await client.post("/api/kyc/create-applicant", headers=auth(user),
                          json={"email": user.email, "first_name": "", "last_name": "B"})
    assert r.status_code == 422
    # provider error -> 502, nothing stored
    sumsub_env.on("POST", "/resources/applicants", 500, {"description": "boom"})
    r = await client.post("/api/kyc/create-applicant", headers=auth(user),
                          json={"email": user.email, "first_name": "A", "last_name": "B"})
    assert r.status_code == 502
    await db.refresh(user)
    assert user.kyc_sumsub_id is None
    # already verified
    user.kyc_status = "verified"
    await db.commit()
    r = await client.post("/api/kyc/create-applicant", headers=auth(user),
                          json={"email": user.email, "first_name": "A", "last_name": "B"})
    assert r.status_code == 400


async def test_create_applicant_route_unconfigured(client, user, sumsub_unconfigured):
    r = await client.post("/api/kyc/create-applicant", headers=auth(user),
                          json={"email": user.email, "first_name": "A", "last_name": "B"})
    assert r.status_code == 503


async def test_applicant_status_route_syncs_own_status(client, db, user, sumsub_env):
    user.kyc_sumsub_id = "app-own"
    await db.commit()
    sumsub_env.on("GET", "/resources/applicants/app-own/status", 200,
                  {"reviewStatus": "completed", "reviewResult": {"reviewAnswer": "GREEN"}})
    r = await client.get("/api/kyc/applicant/app-own/status", headers=auth(user))
    assert r.status_code == 200, r.text
    assert r.json() == {"applicant_id": "app-own", "kyc_status": "verified", "review_status": "completed",
                        "review_answer": "GREEN", "reject_labels": []}
    await db.refresh(user)
    assert user.kyc_status == "verified", "status poll syncs the account"


async def test_applicant_status_route_authorization(client, db, user, admin, sumsub_env):
    sumsub_env.on("GET", "/resources/applicants/app-other/status", 200, {"reviewStatus": "pending"})
    r = await client.get("/api/kyc/applicant/app-other/status", headers=auth(user))
    assert r.status_code == 403, "users cannot query other applicants"
    assert sumsub_env.requests == []
    r = await client.get("/api/kyc/applicant/app-other/status", headers=auth(admin))
    assert r.status_code == 200 and r.json()["kyc_status"] == "pending"
    await db.refresh(admin)
    assert admin.kyc_status == "verified", "looking at someone else's applicant never touches your own status"


async def test_applicant_status_route_provider_error(client, db, user, sumsub_env):
    user.kyc_sumsub_id = "app-err"
    await db.commit()
    sumsub_env.on("GET", "/resources/applicants/app-err/status", 503, {"description": "maintenance"})
    r = await client.get("/api/kyc/applicant/app-err/status", headers=auth(user))
    assert r.status_code == 502


# --------------------------------------------------------------------------- Sumsub webhook

async def test_sumsub_webhook_applies_review(client, db, user, sumsub_env, monkeypatch):
    import api.routes.kyc as kyc_mod
    sent = []

    class Capture:
        async def send_kyc_notification(self, email, status):
            sent.append((email, status))
            return True
    monkeypatch.setattr(kyc_mod, "email_service", Capture())

    user.kyc_sumsub_id = "app-hook"
    await db.commit()
    body = json.dumps({"type": "applicantReviewed", "applicantId": "app-hook", "reviewStatus": "completed",
                       "reviewResult": {"reviewAnswer": "GREEN"}}).encode()
    r = await client.post("/api/webhooks/sumsub", content=body,
                          headers={"x-payload-digest": sign(body), "x-payload-digest-alg": "HMAC_SHA256_HEX",
                                   "content-type": "application/json"})
    assert r.status_code == 200, r.text
    assert r.json() == {"received": True, "type": "applicantReviewed", "applicant_id": "app-hook",
                        "known": True, "kyc_status": "verified", "changed": True}
    await db.refresh(user)
    assert user.kyc_status == "verified" and sent == [(user.email, "verified")]

    # replay: no change, no second email
    r = await client.post("/api/webhooks/sumsub", content=body, headers={"x-payload-digest": sign(body)})
    assert r.status_code == 200 and r.json()["changed"] is False and len(sent) == 1


async def test_sumsub_webhook_rejects_bad_or_missing_signature(client, db, user, sumsub_env):
    user.kyc_sumsub_id = "app-sig"
    await db.commit()
    body = json.dumps({"applicantId": "app-sig", "reviewStatus": "completed",
                       "reviewResult": {"reviewAnswer": "GREEN"}}).encode()
    assert (await client.post("/api/webhooks/sumsub", content=body,
                              headers={"x-payload-digest": "0" * 64})).status_code == 401
    assert (await client.post("/api/webhooks/sumsub", content=body)).status_code == 401
    assert (await client.post("/api/webhooks/sumsub", content=body,
                              headers={"x-payload-digest": sign(body), "x-payload-digest-alg": "NOPE"})).status_code == 401
    await db.refresh(user)
    assert user.kyc_status == "pending"


async def test_sumsub_webhook_unknown_applicant_and_bad_body(client, sumsub_env):
    body = json.dumps({"type": "applicantPending", "applicantId": "nobody", "reviewStatus": "pending"}).encode()
    r = await client.post("/api/webhooks/sumsub", content=body, headers={"x-payload-digest": sign(body)})
    assert r.status_code == 200 and r.json()["known"] is False
    body = b"not json"
    assert (await client.post("/api/webhooks/sumsub", content=body,
                              headers={"x-payload-digest": sign(body)})).status_code == 422
    body = b'{"type": "applicantCreated"}'
    assert (await client.post("/api/webhooks/sumsub", content=body,
                              headers={"x-payload-digest": sign(body)})).status_code == 422


async def test_sumsub_webhook_without_secret_configured_rejects(client, sumsub_unconfigured):
    body = b'{"applicantId": "x"}'
    assert (await client.post("/api/webhooks/sumsub", content=body,
                              headers={"x-payload-digest": sign(body)})).status_code == 401


# --------------------------------------------------------------------------- email routes

async def test_email_routes_require_jwt(client):
    assert (await client.post("/api/email/send-verification", json={"email": "a@b.co"})).status_code == 401
    assert (await client.post("/api/email/send-notification",
                              json={"email": "a@b.co", "subject": "s", "message": "m"})).status_code == 401


async def test_send_verification_route(client, user, resend_env, monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://app.autobuy.test/")
    r = await client.post("/api/email/send-verification", json={"email": user.email.upper()}, headers=auth(user))
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": True, "email_id": "email-abc123"}
    sent = resend_env.last_json()
    assert sent["to"] == [user.email] and sent["subject"] == f"Verify your {settings.app_name} email"
    assert "https://app.autobuy.test/verify-email?token=" in sent["text"]
    assert 'href="https://app.autobuy.test/verify-email?token=' in sent["html"]

    # the emailed token identifies the user via /api/auth/verify but cannot be used as a login credential
    token = sent["text"].split("token=", 1)[1].split()[0]
    r = await client.post("/api/auth/verify", json={"token": token})
    assert r.status_code == 200 and r.json() == {"valid": True, "user_id": str(user.id)}
    assert (await client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})).status_code == 401


def test_verification_link_defaults_to_first_cors_origin(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "")
    monkeypatch.setattr(settings, "allowed_origins_csv", "https://dash.example.com,https://other")
    assert verification_link("t").startswith("https://dash.example.com/verify-email?token=t")
    assert make_verification_token("u1").count(".") == 2


async def test_send_verification_route_guards(client, user, resend_env, resend_unconfigured):
    r = await client.post("/api/email/send-verification", json={"email": "other@example.com"}, headers=auth(user))
    assert r.status_code == 403
    r = await client.post("/api/email/send-verification", json={"email": "not-an-email"}, headers=auth(user))
    assert r.status_code == 422
    r = await client.post("/api/email/send-verification", json={"email": user.email}, headers=auth(user))
    assert r.status_code == 503, "Resend not configured"
    assert resend_env.requests == []


async def test_send_notification_route(client, user, admin, resend_env):
    body = {"email": user.email, "subject": "Heads up", "message": "Line 1\n<b>not bold</b>"}
    r = await client.post("/api/email/send-notification", json=body, headers=auth(user))
    assert r.status_code == 200 and r.json()["email_id"] == "email-abc123"
    sent = resend_env.last_json()
    assert sent["subject"] == "Heads up" and sent["text"] == body["message"]
    assert sent["html"] == "<p>Line 1<br>&lt;b&gt;not bold&lt;/b&gt;</p>", "message is escaped, never rendered"

    # users cannot notify other addresses; admins can
    other = {"email": "customer@example.com", "subject": "s", "message": "m"}
    assert (await client.post("/api/email/send-notification", json=other, headers=auth(user))).status_code == 403
    assert (await client.post("/api/email/send-notification", json=other, headers=auth(admin))).status_code == 200
    assert resend_env.last_json()["to"] == ["customer@example.com"]

    # provider failure -> 502
    resend_env.on("POST", "/emails", 500, {"message": "internal"})
    assert (await client.post("/api/email/send-notification", json=body, headers=auth(user))).status_code == 502


async def test_send_notification_validation(client, user, resend_env):
    bad = [{"email": user.email, "subject": "", "message": "m"},
           {"email": user.email, "subject": "s", "message": ""},
           {"email": user.email, "subject": "s" * 201, "message": "m"},
           {"email": user.email, "subject": "s"}]
    for body in bad:
        assert (await client.post("/api/email/send-notification", json=body, headers=auth(user))).status_code == 422
    assert resend_env.requests == []
