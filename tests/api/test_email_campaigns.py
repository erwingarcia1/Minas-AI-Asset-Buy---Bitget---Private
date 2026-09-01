import pytest

from api.models import Transaction
from api.services import email_campaigns as camp
from api.services.email_templates import TEMPLATES, render
from tests.api.test_email_service import CapturingEmail


def test_all_templates_render_both_formats():
    contexts = {
        "welcome": {"name": "sam"},
        "first_purchase": {"name": "sam", "crypto": "BTC", "amount_crypto": "0.001",
                           "cost_usdt": "100.00", "exchange_rate": "50000.00",
                           "order_id": "123"},
        "purchase_summary": {"name": "sam", "rows": [{"crypto": "BTC", "amount": "0.001",
                             "cost": "100.00"}], "total_usdt": "100.00", "count": 1,
                             "failed": 0},
        "kyc_approved": {"name": "sam"},
        "kyc_rejected": {"name": "sam"},
        "downtime_alert": {"start": "2026-09-05 02:00", "end": "2026-09-05 03:00"},
        "milestone": {"name": "sam", "milestone": "1,000"},
    }
    assert set(contexts) == TEMPLATES
    for name, ctx in contexts.items():
        html, text = render(name, **ctx)
        assert "<html>" in html and "{{" not in html, name
        assert "{{" not in text and text.strip(), name
        for v in ctx.values():
            if isinstance(v, str) and v not in ("sam",):
                assert v in html or v in text, (name, v)


def test_unknown_template_rejected():
    with pytest.raises(KeyError):
        render("phishing_special", name="x")


async def test_first_purchase_email(db, user, purchase, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    tx = Transaction(user_id=user.id, recurring_purchase_id=purchase.id, crypto="BTC",
                     amount_crypto=0.001, cost_usdt=100.0, exchange_rate=50000.0,
                     binance_order_id="1", status="completed")
    db.add(tx)
    await db.commit()
    await camp.after_purchase_completed(db, user, tx)
    assert len(capture.sent) == 1
    assert "first automated purchase" in capture.sent[0]["subject"]


async def test_milestone_crossing(db, user, purchase, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    older = Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.01,
                        cost_usdt=950.0, status="completed")
    crossing = Transaction(user_id=user.id, recurring_purchase_id=purchase.id,
                           crypto="BTC", amount_crypto=0.001, cost_usdt=100.0,
                           status="completed")
    db.add_all([older, crossing])
    await db.commit()
    await camp.after_purchase_completed(db, user, crossing)  # 950 -> 1050 crosses 1k
    assert len(capture.sent) == 1
    assert "1,000 USDT invested" in capture.sent[0]["subject"]

    # a further purchase that crosses nothing sends nothing
    quiet = Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.001,
                        cost_usdt=50.0, status="completed")
    db.add(quiet)
    await db.commit()
    await camp.after_purchase_completed(db, user, quiet)
    assert len(capture.sent) == 1


async def test_welcome_sent_on_register(db, monkeypatch):
    import httpx, uuid
    from sqlalchemy import select
    from api.main import app
    from api.models import User
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    email = f"welcome-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            r = await c.post("/api/auth/register",
                             json={"email": email, "password": "Str0ng!passw0rd"})
            assert r.status_code == 201
        assert capture.sent and capture.sent[0]["subject"] == "Welcome to AutoBuy"
        assert capture.sent[0]["to"] == email
    finally:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u:
            await db.delete(u)
            await db.commit()


async def test_weekly_summary(db, user, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    db.add_all([
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.002,
                    cost_usdt=200.0, status="completed"),
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.1,
                    cost_usdt=150.0, status="completed"),
        Transaction(user_id=user.id, crypto="SOL", amount_crypto=1.0,
                    cost_usdt=999.0, status="failed"),
    ])
    await db.commit()
    await camp.send_weekly_summaries()
    mine = [m for m in capture.sent if m["to"] == user.email]
    assert len(mine) == 1
    body = mine[0]["text"]
    assert "350.00" in body and "BTC" in body and "ETH" in body
    assert "1 failed" in body
