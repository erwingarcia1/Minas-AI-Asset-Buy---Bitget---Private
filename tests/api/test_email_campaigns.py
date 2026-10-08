import pytest

from tests.api.conftest import make_autobuy_purchase, make_history
from api.services import email_campaigns as camp
from api.services.email_templates import TEMPLATES, render
from tests.api.test_email_service import CapturingEmail


def test_all_templates_render_both_formats():
    contexts = {
        "welcome": {"name": "sam"},
        "first_purchase": {"name": "sam", "crypto": "BTC", "amount_crypto": "0.001",
                           "cost_usdt": "100.00", "exchange_rate": "50,000.00",
                           "order_id": "123"},
        "purchase_summary": {"name": "sam", "rows": [{"crypto": "BTC", "amount": "0.001",
                             "cost": "100.00"}], "total_usdt": "100.00", "count": 1,
                             "failed": 0},
        "kyc_approved": {"name": "sam"},
        "kyc_rejected": {"name": "sam"},
        "downtime_alert": {"start": "2026-09-05 02:00", "end": "2026-09-05 03:00"},
        "milestone": {"name": "sam", "milestone": "1,000"},
        "unified_ready": {"greeting": "Hello,", "purchases_url": "https://x.test/purchases"},
        "unified_account": {"greeting": "Hello,", "message": "This Bitget account uses Unified Trading mode, which AutoBuy doesn't support yet.", "paused": 1, "settings_url": "https://x.test/settings", "purchases_url": "https://x.test/purchases"},
        "low_balance": {"greeting": "Hello,", "attempts": 3, "missed_cycles": 3, "purchases": [{"coin": "XRP", "amount_usdt": "10.00", "frequency": "weekly",
                                                      "run_at": "Saturday, Oct 11 at 8:16 PM ET (00:16 UTC)"}],
                        "count": 1, "count_words": "one", "needed_usdt": "10.10", "balance_usdt": "3.50", "shortfall_usdt": "6.60",
                        "exchange": "Bitget", "purchases_url": "https://x.test/purchases",
                        "unsubscribe_url": "https://x.test/settings/notifications"},
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


async def test_first_purchase_email(db, user, binance_key, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    p = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    tx = await make_history(db, user, purchase=p, amount_crypto=0.001, cost_usd=100.0, fill_price=50000.0,
                            order_id="ORD-1")
    await camp.on_buy_executed(db, user, tx, p)
    assert len(capture.sent) == 1
    assert "first automated purchase" in capture.sent[0]["subject"]
    assert "ORD-1" in capture.sent[0]["text"] and "50,000.00" in capture.sent[0]["text"]


async def test_milestone_crossing(db, user, binance_key, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    p = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    await make_history(db, user, purchase=p, amount_crypto=0.01, cost_usd=950.0)
    crossing = await make_history(db, user, purchase=p, amount_crypto=0.001, cost_usd=100.0)
    await camp.on_buy_executed(db, user, crossing, p)  # 950 -> 1050 crosses 1k
    assert len(capture.sent) == 1
    assert "1,000 USDT invested" in capture.sent[0]["subject"]

    # a further purchase that crosses nothing sends nothing
    quiet = await make_history(db, user, purchase=p, amount_crypto=0.001, cost_usd=50.0)
    await camp.on_buy_executed(db, user, quiet, p)
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
        from api.config import settings
        assert capture.sent and capture.sent[0]["subject"] == f"Welcome to {settings.app_name}"
        assert capture.sent[0]["to"] == email
    finally:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u:
            await db.delete(u)
            await db.commit()


async def test_weekly_summary(db, user, binance_key, monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(camp, "email_service", capture)
    await make_history(db, user, crypto="BTC", amount_crypto=0.002, cost_usd=200.0, key=binance_key)
    await make_history(db, user, crypto="ETH", amount_crypto=0.1, cost_usd=150.0, key=binance_key, purchase_type="dca")
    await make_history(db, user, crypto="SOL", amount_crypto=1.0, cost_usd=999.0, key=binance_key, status="failed")
    sent = await camp.send_weekly_summaries()
    assert sent >= 1
    mine = [m for m in capture.sent if m["to"] == user.email]
    assert len(mine) == 1
    body = mine[0]["text"]
    assert "350.00" in body and "BTC" in body and "ETH" in body
    assert "1 failed" in body
