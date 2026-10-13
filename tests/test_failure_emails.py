"""The full customer emails for failed buys, as received (#90/#91): approved
subjects, USDT-only money wording, balance figures, Eastern times, the help
footer, mode-specific steps — and never a venue code."""
import re
from datetime import datetime, timezone

import pytest

from api.services import failure_text as ft
from api.services import purchase_events as ev
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import auth, binance_key, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for

pytestmark = pytest.mark.asyncio


@pytest.fixture
def capture(monkeypatch):
    from api.services import email_service as es_mod
    from tests.api.test_email_service import CapturingEmail
    cap = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", cap)
    return cap


@pytest.fixture
def balances(monkeypatch):
    """Scripted venue balances for the email composer (no real reads)."""
    ctx = ft.BalanceContext(trading=12.30, funding=None)
    monkeypatch.setattr(ft, "read_balance_context", lambda account: ctx)
    return ctx


def refusal(code, message):
    return ps.ExecutionError(f"{code}: {message}", stage="buy", code=code)


def no_codes(text):
    assert not re.search(r"\b(25\d{3}|43\d{3}|40\d{3}|MIN_NOTIONAL|SYMBOL)\b", text), text


async def exhaust(db, purchase, gw, runs=3):
    for _ in range(runs):
        await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)


async def test_insufficient_balance_stopped_email_with_figures(db, user, bitget_key, capture, balances, monkeypatch):
    monkeypatch.setattr(bitget_key, "account_mode", "unified", raising=False)
    gw = FakeGateway(fail_buy_error=refusal("25202", "Insufficient balance"))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="BTC", amount_usd=50,
                            purchase_type="dca", frequency="one_time", next_execution=NOW)
    await exhaust(db, p, gw)
    (mail,) = [m for m in capture.sent if m["template"] == "purchase"]
    assert mail["subject"] == "We've paused your BTC purchase. No money was spent"
    text = mail["text"]
    assert "We tried 3 times to buy 50.00 USDT of BTC on bitget." in text
    assert "Your Unified Trading account didn't have enough USDT." in text
    assert "Nothing was bought and no money left your account." in text
    assert "You have 12.30 USDT. Add about 38.20 USDT more." in text
    assert "We've paused this purchase so it doesn't keep failing quietly" in text
    assert text.endswith("Need help? Reply to this email or ask in the AutoBuy Telegram group.")
    assert "USD " not in text.replace("USDT", ""), "USDT only, never USD"
    no_codes(text)


async def test_insufficient_balance_missed_email_with_funding_hint_and_eastern_time(
        db, user, bitget_key, capture, monkeypatch):
    monkeypatch.setattr(bitget_key, "account_mode", "unified", raising=False)
    monkeypatch.setattr(ft, "read_balance_context",
                        lambda account: ft.BalanceContext(trading=12.30, funding=40.0))
    gw = FakeGateway(fail_buy_error=refusal("25202", "Insufficient balance"))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", amount_usd=50,
                            purchase_type="dca", frequency="weekly", next_execution=NOW)
    await exhaust(db, p, gw)   # weekly: cycle missed, purchase lives on
    (mail,) = [m for m in capture.sent if m["template"] == "purchase"]
    assert mail["subject"] == "Your XRP buy didn't go through. No money was spent"
    text = mail["text"]
    assert "enough to cover this" in text and "From: Funding account" in text
    assert "Your next attempt is " in text
    # Eastern time with the UTC clock in brackets, no ISO dates (owner rule 1)
    assert re.search(r"Your next attempt is \w+, \w{3} \d{1,2} at \d{1,2}:\d{2} [AP]M ET \(\d{2}:\d{2} UTC\)", text), text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text), "no ISO dates in customer text"
    no_codes(text)


async def test_permission_email_shows_only_the_right_mode(db, user, bitget_key, capture, balances, monkeypatch):
    gw = FakeGateway(fail_buy_error=refusal("40014", "Incorrect permissions"))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XLM", amount_usd=15,
                            purchase_type="dca", frequency="one_time", next_execution=NOW)
    await exhaust(db, p, gw)
    (mail,) = [m for m in capture.sent if m["template"] == "purchase"]
    assert "allow Spot trading" in mail["text"]           # classic key
    assert "Management → Read" not in mail["text"], "never both modes at once"
    no_codes(mail["text"])


async def test_unknown_error_email_fallback(db, user, bitget_key, capture, balances):
    gw = FakeGateway(fail_buy_error=refusal("99999", "weird new thing"),
                     lookup_states=["not_found", "not_found"] * 3)
    p = await make_purchase(db, user, bitget_key, crypto_symbol="SOL", amount_usd=20,
                            purchase_type="dca", frequency="one_time", next_execution=NOW)
    await exhaust(db, p, gw)
    (mail,) = [m for m in capture.sent if m["template"] == "purchase"]
    assert "made sure no order went through" in mail["text"]
    assert "99999" not in mail["text"], "unknown venue codes stay out of email too"


async def test_raw_codes_still_reach_ops(db, user, bitget_key, capture, balances, monkeypatch):
    """Telegram and the stored row keep the venue string untouched."""
    sent = []

    async def fake_send(text, key=None):
        sent.append(text)
        return True
    from api.services import telegram
    monkeypatch.setattr(telegram, "send", fake_send)
    gw = FakeGateway(fail_buy_error=refusal("25202", "Insufficient balance"))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", amount_usd=2,
                            purchase_type="dca", frequency="one_time", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.error_message == "25202: Insufficient balance", "row keeps the raw string"
    assert any("25202" in t for t in sent), "ops Telegram keeps the code"


async def test_app_shows_plain_lines_but_admin_keeps_raw(db, user, bitget_key):
    """The purchase list/detail a customer sees carries the plain line; the admin
    row keeps the raw venue string."""
    from api.models import PurchaseTransaction, utcnow
    raw = "PURCHASE STOPPED after 3 failed attempts: 25202: Insufficient balance"
    p = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", amount_usd=2,
                            purchase_type="dca", frequency="one_time",
                            next_execution=None, status="failed", last_error=raw)
    tx = PurchaseTransaction(purchase_id=p.id, tx_type="buy", status="failed",
                             client_oid="ab-plain-line-test-000001",
                             error_message="25202: Insufficient balance", executed_at=utcnow())
    db.add(tx)
    await db.commit()

    import httpx
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        rows = (await c.get("/api/v2/purchases", headers=auth(user))).json()
        mine = next(r for r in rows if r["id"] == str(p.id))
        assert mine["last_error"] == ("Not enough USDT in your Bitget account. "
                                      "Top up and the next attempt runs normally.")
        detail = (await c.get(f"/api/v2/purchases/{p.id}", headers=auth(user))).json()
        buys = [t for t in detail["transactions"] if t["tx_type"] == "buy"]
        assert buys[0]["error_message"] == mine["last_error"]
        assert "25202" not in str(detail), "no venue code anywhere in the customer payload"

    from api.routes.admin.purchases import _purchase_row
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from api.models import Purchase
    fresh = (await db.execute(select(Purchase).options(
        selectinload(Purchase.transactions), selectinload(Purchase.user),
        selectinload(Purchase.exchange_account))
        .where(Purchase.id == p.id))).scalar_one()
    assert _purchase_row(fresh).last_error == raw, "admin view keeps the raw string"


async def test_unknown_last_error_passes_through_to_the_app(db, user, bitget_key):
    raw = "sending 1.2 XRP to r... failed: address not in whitelist"
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", amount_usd=10,
                            purchase_type="autobuy", status="withdrawal_failed",
                            next_execution=None, last_error=raw)
    import httpx
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        rows = (await c.get("/api/v2/purchases", headers=auth(user))).json()
        mine = next(r for r in rows if r["id"] == str(p.id))
    assert mine["last_error"] == raw, "withdrawal wording is never relabelled as a buy failure"
