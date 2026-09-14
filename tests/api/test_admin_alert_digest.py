"""Critical admin alerts reach the support inbox and every admin by email,
with or without Slack, at most once per condition every six hours."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.config import settings
from api.integrations import resend
from api.models import User
from api.services import admin_alerts
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import BuyFails
from tests.test_purchase_scheduler import NOW


@pytest.fixture(autouse=True)
async def clean_alert_keys():
    from api.services.redis_client import get_redis
    r = get_redis()
    keys = [k async for k in r.scan_iter("autobuy:alerts:emailed:*")]
    if keys:
        await r.delete(*keys)
    yield
    keys = [k async for k in r.scan_iter("autobuy:alerts:emailed:*")]
    if keys:
        await r.delete(*keys)


@pytest.fixture
async def admin(db):
    u = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def sent(monkeypatch):
    box = []

    async def fake_send(to_email, subject, html_content, **kw):
        box.append({"to": to_email, "subject": subject, "text": kw.get("text_content", ""), "template": kw.get("template")})
        return "email-1"
    monkeypatch.setattr(resend, "send_email", fake_send)
    return box


async def test_nothing_is_emailed_while_no_alert_is_critical(db, sent, monkeypatch):
    monkeypatch.setattr(settings, "support_email", "support@example.com")
    result = await admin_alerts.notify_critical_alerts(db)
    assert "stopped_purchases" not in result["critical"] and result["emailed"] == [] and sent == []


async def test_a_stopped_purchase_reaches_support_and_admins_by_email_once_per_six_hours(db, user, bitget_key, admin, sent, monkeypatch):
    monkeypatch.setattr(settings, "support_email", "support@example.com")
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, frequency="one_time", next_execution=NOW)
    for i in range(3):
        await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5 * i), gateway=BuyFails(), dry_run=True)
    sent.clear()   # the customer's own "stopped" email is not what this test is about
    result = await admin_alerts.notify_critical_alerts(db)
    assert "stopped_purchases" in result["critical"]
    assert set(result["recipients"]) == {"support@example.com", admin.email} and set(result["emailed"]) == set(result["recipients"])
    mail = [m for m in sent if m["template"] == "admin_alert"]
    assert len(mail) == 2 and all("CRITICAL: " in m["subject"] and "stopped_purchases" in m["subject"] for m in mail)
    assert "CRITICAL stopped_purchases = 1" in mail[0]["text"] and "Alerts tab" in mail[0]["text"]
    # the same condition is not emailed again within the repeat window
    again = await admin_alerts.notify_critical_alerts(db)
    assert "stopped_purchases" in again["critical"] and again["emailed"] == []
    assert len([m for m in sent if m["template"] == "admin_alert"]) == 2
    # a NEW critical condition is emailed even while the first is still muted
    from api.models import Purchase
    parked = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, status="needs_review", next_execution=None)
    third = await admin_alerts.notify_critical_alerts(db)
    assert "purchases_needing_review" in third["critical"] and third["emailed"], "new condition -> new email"
    assert any("purchases_needing_review" in m["subject"] for m in sent if m["template"] == "admin_alert")
    assert p.id != parked.id


async def test_alerts_route_and_service_agree(db, admin, client):
    from tests.api.conftest import auth_headers
    via_service = {a.name: a.severity for a in await admin_alerts.compute_alerts(db)}
    via_route = {a["name"]: a["severity"] for a in (await client.get("/api/admin/alerts", headers=auth_headers(admin))).json()}
    assert via_service.keys() == via_route.keys()


def test_scheduler_has_the_alert_digest_job():
    sched = ps.build_scheduler()
    job = sched.get_job("admin_alert_digest")
    assert job is not None and job.func is ps.admin_alert_digest
