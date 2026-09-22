"""Audit C2: password reset, password changed and wallet-hold-lifted emails go
through the outbox under the "security" category (6 attempts, backoff
30 s … 8 min) instead of one inline Resend call that a provider blip lost.

Delivery latency changes from "inline during the request" to "on the next
drain": the drain_email_outbox job runs every minute, so a queued security
email leaves within about 60 seconds; on a provider failure it is retried
instead of dropped. The reset link and token are built exactly as before.
"""
import uuid

from jose import jwt
import pytest
from sqlalchemy import select

from api.auth import hash_password
from api.config import settings
from api.integrations import resend
from api.models import User
from api.services import email_outbox
from tests.conftest import flush_test_redis


@pytest.fixture(autouse=True)
def clean_outbox():
    flush_test_redis()
    yield
    flush_test_redis()


@pytest.fixture
async def account(db):
    u = User(email=f"sec-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password("Old-password-1!"), kyc_status="pending")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def delivered(monkeypatch):
    box = []

    async def fake_send(to_email, subject, html_content, from_email=None, **kw):
        box.append({"to": to_email, "subject": subject, "text": kw.get("text_content", ""), "template": kw.get("template")})
        return "email-1"
    monkeypatch.setattr(resend, "send_email", fake_send)
    return box


async def test_password_reset_is_queued_as_security_and_delivered_by_the_drain(client, account, delivered):
    r = await client.post("/api/auth/forgot-password", json={"email": account.email})
    assert r.status_code == 200 and delivered == [], "nothing is sent inline any more"
    queued = await email_outbox.pending(limit=10)
    assert len(queued) == 1 and queued[0]["template"] == "password_reset" and queued[0]["category"] == "security"
    assert email_outbox.max_attempts(queued[0]["category"]) == 6
    counts = await email_outbox.drain()
    assert counts["sent"] == 1 and len(delivered) == 1
    text = delivered[0]["text"]
    assert f"{settings.app_base_url}/reset-password?token=" in text
    token = text.split("token=", 1)[1].split()[0]
    decoded = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    assert decoded["type"] == "password_reset" and decoded["sub"] == str(account.id), "same token as before: type, subject, 1h TTL"
    assert decoded["exp"] - decoded["iat"] == 3600


async def test_provider_failure_is_retried_not_lost(client, account, monkeypatch):
    attempts = []

    async def failing_send(*a, **kw):
        attempts.append(1)
        raise resend.ResendError("HTTP 429 daily_quota_exceeded")
    monkeypatch.setattr(resend, "send_email", failing_send)
    await client.post("/api/auth/forgot-password", json={"email": account.email})
    counts = await email_outbox.drain()
    assert counts["retried"] == 1 and attempts == [1]
    queued = await email_outbox.pending(limit=10)
    assert len(queued) == 1 and queued[0]["attempts"] == 1 and queued[0]["next_attempt_at"] > queued[0]["queued_at"]
    assert queued[0]["category"] == "security", "still queued for the next of 6 attempts, not dropped"
