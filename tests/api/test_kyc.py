import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

import api.routes.kyc as kyc_mod
from api.config import settings
from api.models import User
from api.routes.user import ProfileUpdateRequest
from api.services.sumsub_client import map_review_to_kyc_status, verify_webhook_signature
from tests.api.test_email_service import CapturingEmail

WEBHOOK_SECRET = "test-webhook-secret"


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_review_mapping():
    assert map_review_to_kyc_status("completed", "GREEN") == "verified"
    assert map_review_to_kyc_status("completed", "RED") == "rejected"
    assert map_review_to_kyc_status("pending", None) == "pending"
    assert map_review_to_kyc_status("init", "GREEN") == "pending"


def test_signature_verification(monkeypatch):
    monkeypatch.setattr(settings, "sumsub_webhook_secret", WEBHOOK_SECRET)
    body = b'{"applicantId": "abc"}'
    assert verify_webhook_signature(body, sign(body))
    assert not verify_webhook_signature(body, "deadbeef")
    assert not verify_webhook_signature(body, "")
    monkeypatch.setattr(settings, "sumsub_webhook_secret", "")
    assert not verify_webhook_signature(body, sign(body)), "no secret configured -> reject"


def test_profile_update_cannot_touch_kyc_status():
    assert set(ProfileUpdateRequest.model_fields) == {"email"}, \
        "kyc_status must not be user-writable"


@pytest.fixture
def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_webhook_updates_status_and_notifies(db, user, monkeypatch, client):
    monkeypatch.setattr(settings, "sumsub_webhook_secret", WEBHOOK_SECRET)
    capture = CapturingEmail()
    monkeypatch.setattr(kyc_mod, "email_service", capture)
    user.kyc_status = "pending"
    user.kyc_sumsub_id = f"applicant-{user.id.hex[:8]}"
    await db.commit()

    body = json.dumps({"applicantId": user.kyc_sumsub_id,
                       "reviewStatus": "completed",
                       "reviewResult": {"reviewAnswer": "GREEN"}}).encode()
    async with client as c:
        r = await c.post("/api/kyc/webhook", content=body,
                         headers={"x-payload-digest": sign(body),
                                  "content-type": "application/json"})
    assert r.status_code == 200, r.text
    fresh = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    await db.refresh(fresh)
    assert fresh.kyc_status == "verified"
    assert capture.sent and capture.sent[0]["subject"] == "✓ Identity verified"


async def test_webhook_rejects_bad_signature(db, user, monkeypatch, client):
    monkeypatch.setattr(settings, "sumsub_webhook_secret", WEBHOOK_SECRET)
    user.kyc_status = "pending"
    user.kyc_sumsub_id = f"applicant-{user.id.hex[:8]}"
    await db.commit()
    body = json.dumps({"applicantId": user.kyc_sumsub_id,
                       "reviewStatus": "completed",
                       "reviewResult": {"reviewAnswer": "GREEN"}}).encode()
    async with client as c:
        r = await c.post("/api/kyc/webhook", content=body,
                         headers={"x-payload-digest": "0" * 64,
                                  "content-type": "application/json"})
    assert r.status_code == 401
    fresh = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    await db.refresh(fresh)
    assert fresh.kyc_status == "pending", "bad signature must not change status"
