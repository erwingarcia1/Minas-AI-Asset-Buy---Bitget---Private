from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

import api.services.notifications as nf_mod
from api.models import Notification
from api.services.notifications import (
    NotificationDispatcher, Preferences, in_quiet_hours)
from tests.api.conftest import auth_headers


class CaptureChannel:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send_sms(self, phone, message):
        self.sent.append(("sms", phone, message))
        return self.ok

    async def send_push(self, token, title, body, data=None):
        self.sent.append(("push", token, title))
        return self.ok

    async def _send(self, to, subject, html, text, template="generic"):
        self.sent.append(("email", to, subject))
        return self.ok


@pytest.fixture
def channels(monkeypatch):
    cap = CaptureChannel()
    monkeypatch.setattr(nf_mod, "email_service", cap)
    monkeypatch.setattr(nf_mod, "sms_service", cap)
    monkeypatch.setattr(nf_mod, "push_service", cap)
    return cap


def noon(monkeypatch):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(nf_mod, "datetime", FakeDT)


def test_quiet_hours_window_math():
    prefs = Preferences(quiet_hours_start=21, quiet_hours_end=8)
    assert in_quiet_hours(prefs, datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc))
    assert in_quiet_hours(prefs, datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc))
    assert not in_quiet_hours(prefs, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
    day = Preferences(quiet_hours_start=9, quiet_hours_end=17)
    assert in_quiet_hours(day, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
    assert not in_quiet_hours(day, datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc))


async def test_preferences_respected(db, user, channels, monkeypatch):
    noon(monkeypatch)
    user.phone_number = "+14155550123"
    user.device_token = "device-1"
    user.notification_preferences = Preferences(
        email_enabled=True, sms_enabled=False, push_enabled=True).to_json()
    await db.commit()
    results = await NotificationDispatcher().notify(
        db, user, "test", "Subject", "Body")
    assert results == {"email": "sent", "sms": "skipped_pref", "push": "sent"}
    kinds = [s[0] for s in channels.sent]
    assert kinds == ["email", "push"], "disabled channel must never fire"


async def test_quiet_hours_suppress_interruptive_channels(db, user, channels, monkeypatch):
    class NightDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(nf_mod, "datetime", NightDT)
    user.phone_number = "+14155550123"
    user.device_token = "device-1"
    user.notification_preferences = Preferences(sms_enabled=True).to_json()
    await db.commit()
    results = await NotificationDispatcher().notify(db, user, "test", "S", "B")
    assert results["sms"] == "skipped_quiet" and results["push"] == "skipped_quiet"
    assert results["email"] == "sent", "email is not interruptive; quiet hours don't apply"
    critical = await NotificationDispatcher().notify(
        db, user, "alert", "S", "B", critical=True)
    assert critical["sms"] == "sent" and critical["push"] == "sent", \
        "critical events override quiet hours"


async def test_history_audit_rows(db, user, channels, monkeypatch):
    noon(monkeypatch)
    await NotificationDispatcher().notify(db, user, "test", "Audited", "B")
    rows = (await db.execute(select(Notification).where(
        Notification.user_id == user.id))).scalars().all()
    assert {r.channel for r in rows} == {"email", "sms", "push"}
    assert all(r.subject == "Audited" for r in rows)


async def test_preferences_routes_and_validation(db, user, channels, monkeypatch):
    noon(monkeypatch)
    from api.main import app
    H = auth_headers(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        bad = await c.put("/api/notifications/preferences", headers=H,
                          json={"phone_number": "555-1234"})
        assert bad.status_code == 422
        r = await c.put("/api/notifications/preferences", headers=H,
                        json={"sms_enabled": True, "phone_number": "+14155550123",
                              "device_token": "tok-1", "quiet_hours_start": 22})
        assert r.status_code == 200
        body = r.json()
        assert body["sms_enabled"] is True and body["has_device_token"] is True
        assert body["quiet_hours_start"] == 22

        test = await c.post("/api/notifications/test", headers=H)
        assert test.json()["channels"]["sms"] == "sent"

        hist = (await c.get("/api/notifications/history", headers=H)).json()
        assert hist["total"] >= 3


async def test_large_purchase_is_critical_sms(db, user, purchase, binance_key,
                                              channels, monkeypatch):
    class NightDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(nf_mod, "datetime", NightDT)
    from api.services import purchase_service as ps_mod
    from api.services.purchase_service import PurchaseService
    from tests.api.test_purchase_service import FakeBinance
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    user.phone_number = "+14155550123"
    user.notification_preferences = Preferences(sms_enabled=True).to_json()
    purchase.amount_usdt = 1500.0  # over the large-transaction threshold
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"
    sms = [s for s in channels.sent if s[0] == "sms"]
    assert sms, "large transactions page through quiet hours"
    assert "1500.00 USDT" in sms[0][2]
