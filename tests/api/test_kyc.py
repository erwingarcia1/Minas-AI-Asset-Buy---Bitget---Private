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


# --------------------------------------------------------------------------- GET /api/kyc/status sync rules

import api.integrations.sumsub as sumsub_mod
from tests.api.conftest import auth_headers


def _sumsub_answer(monkeypatch, review_status, answer, **extra):
    """Sumsub configured + a canned applicant status; records how often it was asked."""
    calls = []

    async def fake_status(applicant_id):
        calls.append(applicant_id)
        rr = {"reviewAnswer": answer, **extra} if answer else None
        return {"applicant_id": applicant_id, "review_status": review_status, "review_answer": answer,
                "reject_labels": list((rr or {}).get("rejectLabels", [])),
                "kyc_status": map_review_to_kyc_status(review_status, answer),
                "raw": {"reviewStatus": review_status, "reviewResult": rr}}
    monkeypatch.setattr(sumsub_mod, "is_configured", lambda: True)
    monkeypatch.setattr(sumsub_mod, "get_applicant_status", fake_status)
    return calls


async def test_status_syncs_from_sumsub_while_pending(db, user, monkeypatch, client):
    user.kyc_status, user.kyc_sumsub_id = "pending", "app-sync-1"
    await db.commit()
    calls = _sumsub_answer(monkeypatch, "completed", "GREEN")
    async with client as c:
        r = await c.get("/api/kyc/status", headers=auth_headers(user))
    assert r.status_code == 200
    body = r.json()
    assert body["kyc_status"] == "verified" and body["synced"] is True and body["review_status"] == "completed"
    assert calls == ["app-sync-1"]
    fresh = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    await db.refresh(fresh)
    assert fresh.kyc_status == "verified", "users.kyc_status updated without the webhook"


@pytest.mark.parametrize("final", ["verified", "rejected"])
async def test_status_does_not_call_sumsub_when_stored_status_is_final(db, user, monkeypatch, client, final):
    user.kyc_status, user.kyc_sumsub_id = final, "app-final-1"
    await db.commit()
    calls = _sumsub_answer(monkeypatch, "completed", "GREEN")
    async with client as c:
        r = await c.get("/api/kyc/status", headers=auth_headers(user))
    assert r.status_code == 200 and r.json()["kyc_status"] == final and r.json()["synced"] is False
    assert calls == [], "final stored status must not trigger a Sumsub call on page load"


async def test_status_sync_flag_forces_sumsub_and_returns_rejection_details(db, user, monkeypatch, client):
    user.kyc_status, user.kyc_sumsub_id = "rejected", "app-retry-1"
    await db.commit()
    calls = _sumsub_answer(monkeypatch, "completed", "RED", rejectLabels=["UNSATISFACTORY_PHOTOS"],
                           moderationComment="The photo is blurry, please retake it.", reviewRejectType="RETRY")
    async with client as c:
        r = await c.get("/api/kyc/status", params={"sync": 1}, headers=auth_headers(user))
    body = r.json()
    assert calls == ["app-retry-1"] and body["synced"] is True
    assert body["kyc_status"] == "rejected" and body["reject_labels"] == ["UNSATISFACTORY_PHOTOS"]
    assert body["rejection_reason"].startswith("The photo is blurry") and body["retryable"] is True
    # a resubmission moves the applicant back to pending on the next sync
    _sumsub_answer(monkeypatch, "pending", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=__import__("api.main").main.app), base_url="http://test") as c:
        r = await c.get("/api/kyc/status", params={"sync": 1}, headers=auth_headers(user))
    assert r.json()["kyc_status"] == "pending" and r.json()["review_status"] == "pending"


async def test_status_without_sumsub_configured_returns_stored(db, user, monkeypatch, client):
    user.kyc_status, user.kyc_sumsub_id = "pending", "app-unconf-1"
    await db.commit()
    monkeypatch.setattr(sumsub_mod, "is_configured", lambda: False)
    async with client as c:
        r = await c.get("/api/kyc/status", headers=auth_headers(user))
    assert r.status_code == 200 and r.json() == {"kyc_status": "pending", "applicant_id": "app-unconf-1", "review_status": None,
                                                 "reject_labels": [], "rejection_reason": None, "retryable": True, "synced": False}
