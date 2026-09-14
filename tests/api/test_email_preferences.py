"""Notification settings: optional email categories the user can switch off,
the weekly-summary unsubscribe link, and the emails that are never optional."""
import json

import httpx
import pytest

from api.services import email_campaigns as camp
from api.services.notifications import ALWAYS_ON_EMAILS, OPTIONAL_EMAIL_CATEGORIES, Preferences
from tests.api.conftest import auth_headers, make_history
from tests.api.test_email_service import CapturingEmail


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_preferences_default_on_and_only_optional_categories_can_be_off():
    p = Preferences()
    assert all(p.wants_email(c) for c in OPTIONAL_EMAIL_CATEGORIES)
    p.email_purchase_confirmations = False
    assert not p.wants_email("email_purchase_confirmations")
    assert p.wants_email("password_changed") and p.wants_email("purchase_failed"), "unknown / security categories are always on"
    assert set(OPTIONAL_EMAIL_CATEGORIES) == {"email_purchase_confirmations", "email_weekly_summary", "email_milestones"}
    assert "password reset" in ALWAYS_ON_EMAILS and "purchase failed" in ALWAYS_ON_EMAILS and "withdrawal failed" in ALWAYS_ON_EMAILS


async def test_preferences_api_round_trip(db, user):
    H = auth_headers(user)
    async with client() as c:
        before = (await c.get("/api/notifications/preferences", headers=H)).json()
        assert before["email_purchase_confirmations"] is True and before["email_weekly_summary"] is True and before["email_milestones"] is True
        assert before["always_on_emails"] == list(ALWAYS_ON_EMAILS)
        r = await c.put("/api/notifications/preferences", headers=H, json={"email_weekly_summary": False, "email_milestones": False})
        assert r.status_code == 200
        body = r.json()
        assert body["email_weekly_summary"] is False and body["email_milestones"] is False and body["email_purchase_confirmations"] is True
        again = (await c.get("/api/notifications/preferences", headers=H)).json()
        assert again["email_weekly_summary"] is False, "stored per user"
    await db.refresh(user)
    assert json.loads(user.notification_preferences)["email_weekly_summary"] is False


async def test_weekly_summary_honours_the_opt_out_and_carries_the_unsubscribe_link(db, user, binance_key, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    monkeypatch.setattr(camp.settings, "app_base_url", "https://app.example.com")
    await make_history(db, user, crypto="BTC", amount_crypto=0.002, cost_usd=200.0, key=binance_key)
    await camp.send_weekly_summaries()
    mine = [m for m in capture.sent if m["to"] == user.email]
    assert len(mine) == 1
    assert "https://app.example.com/settings/notifications" in mine[0]["text"]
    assert "Unsubscribe or change which emails you get" in mine[0]["text"]
    assert "Security and failure alerts are always sent" in mine[0]["text"]
    # opted out: no digest for this user
    user.notification_preferences = Preferences(email_weekly_summary=False).to_json()
    await db.commit()
    capture.sent.clear()
    await camp.send_weekly_summaries()
    assert [m for m in capture.sent if m["to"] == user.email] == []


async def test_purchase_confirmations_can_be_switched_off_but_failures_cannot(db, user, binance_key, monkeypatch):
    from api.services import email_service as es_mod
    from api.tasks import purchase_scheduler as ps
    from tests.conftest_autobuy import make_purchase
    from tests.fake_gateway import BuyFails, FakeGateway
    from tests.test_purchase_scheduler import NOW
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    user.notification_preferences = Preferences(email_purchase_confirmations=False).to_json()
    await db.commit()
    ok = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    assert [m for m in capture.sent if m["template"] == "purchase"] == [], "confirmation opted out"
    bad = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    failures = [m for m in capture.sent if m["template"] == "purchase"]
    assert len(failures) == 1 and "Purchase failed" in failures[0]["subject"], "failure emails are never optional"
    assert ok.id != bad.id


async def test_milestone_emails_can_be_switched_off(db, user, binance_key, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    from tests.api.conftest import make_autobuy_purchase
    p = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    tx = await make_history(db, user, purchase=p, amount_crypto=0.001, cost_usd=100.0, fill_price=50000.0, order_id="ORD-1")
    user.notification_preferences = Preferences(email_milestones=False).to_json()
    await db.commit()
    await camp.on_buy_executed(db, user, tx, p)
    assert capture.sent == []


async def test_password_changed_email_ignores_every_opt_out(db, user, monkeypatch):
    """Security emails do not go through the preference gate at all."""
    from api.routes import auth as auth_mod
    from datetime import datetime, timezone
    box = []

    async def fake_send(to_email, subject, html_content, **kw):
        box.append(subject)
        return "id"
    monkeypatch.setattr(auth_mod.resend, "send_email", fake_send)
    user.notification_preferences = json.dumps({"email_enabled": False, "email_purchase_confirmations": False,
                                                "email_weekly_summary": False, "email_milestones": False})
    await db.commit()
    await auth_mod._send_password_changed_email(user.email, datetime.now(timezone.utc), "127.0.0.1")
    assert box and "password was changed" in box[0]
