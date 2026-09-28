"""POST /api/admin/telegram/test: admin-only, rate limited, reports the
outcome without ever echoing the token."""
import uuid

import httpx
import pytest
from sqlalchemy import select

from api.config import settings
from api.models import User
from api.services import telegram
from tests.api.conftest import auth_headers

TOKEN = "123456789:AAH-s3cr3t-t0ken-value-XYZ"


@pytest.fixture
async def admin(db):
    u = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def fake_telegram(monkeypatch):
    calls = {"requests": [], "status": 200, "body": {"ok": True, "result": {"message_id": 1}}}

    def handler(request):
        calls["requests"].append(request)
        return httpx.Response(calls["status"], json=calls["body"])
    monkeypatch.setattr(telegram, "transport_factory", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_chat_id", "-100777")
    monkeypatch.setattr(settings, "telegram_max_per_minute", 20)
    telegram.reset_state()
    yield calls
    telegram.reset_state()


async def test_admin_test_message_reports_success_without_secrets(client, admin, fake_telegram):
    async with client as c:
        r = await c.post("/api/admin/telegram/test", headers=auth_headers(admin))
    assert r.status_code == 200 and r.json() == {"configured": True, "sent": True, "error": None}
    assert TOKEN not in r.text and "s3cr3t" not in r.text
    import json
    (req,) = fake_telegram["requests"]
    text = json.loads(req.content)["text"]
    assert "test message" in text and admin.email not in text and telegram.mask_email(admin.email) in text


async def test_admin_test_message_surfaces_telegrams_error(client, admin, fake_telegram):
    fake_telegram["status"], fake_telegram["body"] = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
    async with client as c:
        r = await c.post("/api/admin/telegram/test", headers=auth_headers(admin))
    assert r.status_code == 200 and r.json() == {"configured": True, "sent": False, "error": "HTTP 401: Unauthorized"}
    assert TOKEN not in r.text


async def test_admin_test_message_says_when_unconfigured(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    async with client as c:
        r = await c.post("/api/admin/telegram/test", headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["configured"] is False and r.json()["sent"] is False
    assert "not both set" in r.json()["error"]


async def test_non_admin_is_refused(client, user, fake_telegram):
    async with client as c:
        r = await c.post("/api/admin/telegram/test", headers=auth_headers(user))
    assert r.status_code == 403 and fake_telegram["requests"] == []
