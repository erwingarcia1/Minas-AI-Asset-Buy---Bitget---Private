"""Telegram ops alerts: send success, failure never propagates, unset config
is a no-op, secrets and addresses are masked, the rate limiter and the burst
collapse, the executor's outcome is identical with Telegram disabled,
enabled and failing, and the withdrawal poller's confirmation message."""
import logging
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from api.config import settings
from api.models import PurchaseTransaction
from api.services import purchase_events as ev
from api.services import telegram
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import BuyFails, FakeGateway, RichGateway
from tests.test_purchase_scheduler import NOW
from tests.test_withdrawal_ops import sent_withdrawal

TOKEN = "123456789:AAH-s3cr3t-t0ken-value-XYZ"
CHAT = "-1001234567890"


class TG:
    """A fake Telegram: records requests, answers as configured."""
    def __init__(self, status=200, body=None, raise_times=0):
        self.requests, self.status, self.body, self.raise_times = [], status, body, raise_times

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_times > 0:
            self.raise_times -= 1
            raise httpx.ConnectTimeout("boom", request=request)
        return httpx.Response(self.status, json=self.body or {"ok": True, "result": {"message_id": len(self.requests)}})

    def texts(self):
        import json
        return [json.loads(r.content)["text"] for r in self.requests]


@pytest.fixture
def tg(monkeypatch):
    fake = TG()
    monkeypatch.setattr(telegram, "transport_factory", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_chat_id", CHAT)
    monkeypatch.setattr(settings, "telegram_max_per_minute", 20)
    telegram.reset_state()
    yield fake
    telegram.reset_state()


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    telegram.reset_state()


# --------------------------------------------------------------------------- send

async def test_send_posts_to_the_bot_api_with_the_chat_id(tg):
    assert await telegram.send("hello") is True
    (req,) = tg.requests
    assert req.method == "POST" and req.url.path == f"/bot{TOKEN}/sendMessage"
    import json
    body = json.loads(req.content)
    assert body == {"chat_id": CHAT, "text": "hello", "disable_web_page_preview": True}


async def test_unset_config_is_a_silent_no_op(unset, monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "transport_factory", lambda: calls.append(1) or None)
    assert telegram.configured() is False
    assert await telegram.send("hello") is False
    assert await telegram.flush() == 0
    assert calls == [], "no HTTP client is even built"


async def test_placeholder_token_counts_as_unset(tg, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "your_bot_token_here")
    assert telegram.configured() is False
    assert await telegram.send("hello") is False and tg.requests == []


async def test_failures_never_propagate_and_never_log_the_token(tg, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    # Telegram rejects: no retry, False, one warning with the description
    tg.status, tg.body = 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    res = await telegram.send_detailed("x")
    assert res.ok is False and res.error == "HTTP 400: Bad Request: chat not found" and len(tg.requests) == 1
    # transport error: exactly one retry, then False
    tg.status, tg.body, tg.raise_times = 200, None, 5
    assert await telegram.send("y") is False and len(tg.requests) == 3
    # a formatter bug inside send never escapes
    monkeypatch.setattr(telegram, "_post", None)   # calling None raises TypeError
    assert await telegram.send("z") is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 3
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "chat not found" in everything
    assert TOKEN not in everything and "s3cr3t" not in everything, "the bot token must never reach a log line"
    assert "bot<redacted>" in everything, "httpx's request-URL log line is scrubbed, not silenced"


async def test_one_retry_on_a_transport_error_then_success(tg):
    tg.raise_times = 1
    assert await telegram.send("retry me") is True and len(tg.requests) == 2


# --------------------------------------------------------------------------- masking

def test_masking_rules():
    assert telegram.mask_email("support@minastech.ca") == "su…@minastech.ca"
    assert telegram.mask_email("a@b.co") == "a…@b.co"
    assert telegram.mask_email(None) == "unknown"
    assert telegram.mask_address("rN7n7otQDd6FczFgLdlqtyMVrn3Nv5Whqx") == "rN7n7o…Whqx"
    assert telegram.mask_address("GDQP2KPQGKIHYJGXNUIYOMHARUARCA7DJT5FO2FFOOKY3B2WSQHG4W37") == "GDQP2K…4W37"
    assert telegram.mask_address("short") == "sho…" and telegram.mask_address(None) == "—"
    reason = telegram.scrub("43012: address rN7n7otQDd6FczFgLdlqtyMVrn3Nv5Whqx for jane.doe@example.com not in whitelist; key bg_1234567890abcdefghijklmnopqrstuv rejected")
    assert "rN7n7otQDd6FczFgLdlqtyMVrn3Nv5Whqx" not in reason and "jane.doe@example.com" not in reason
    assert "bg_1234567890abcdefghijklmnopqrstuv" not in reason
    assert reason.startswith("43012: address rN7n7o…Whqx for ja…@example.com")
    assert telegram.scrub("") == "no reason given" and len(telegram.scrub("word " * 100)) == 160


# --------------------------------------------------------------------------- rate limit + collapse

async def test_rate_limit_drops_beyond_n_per_minute(tg, monkeypatch):
    monkeypatch.setattr(settings, "telegram_max_per_minute", 3)
    t = [1000.0]
    monkeypatch.setattr(telegram, "clock", lambda: t[0])
    results = [await telegram.send(f"m{i}") for i in range(5)]
    assert results == [True, True, True, False, False] and len(tg.requests) == 3
    assert telegram.dropped_total == 2
    t[0] += 61
    assert await telegram.send("later") is True and len(tg.requests) == 4, "the window slides"


async def test_identical_failures_collapse_into_one_message_with_a_count(tg, monkeypatch):
    t = [5000.0]
    monkeypatch.setattr(telegram, "clock", lambda: t[0])
    for _ in range(6):
        await telegram.send("❌ Purchase failed\nBTC · 50.00 USDT\nReason: 43012: Insufficient balance", key="buy_failed:43012")
    assert len(tg.requests) == 1, "one message for the burst"
    await telegram.send("✅ other thing", key="other")
    assert len(tg.requests) == 2
    # end of the scheduler run: the suppressed count goes out once
    assert await telegram.flush() == 1
    assert tg.texts()[-1] == "❌ Purchase failed\n↳ repeated 5 more times in this run"
    assert await telegram.flush() == 0, "counts reset"
    # after the window a new identical failure sends again, carrying any count still pending
    await telegram.send("❌ Purchase failed\nBTC · 50.00 USDT\nReason: 43012: Insufficient balance", key="buy_failed:43012")
    t[0] += 61
    await telegram.send("❌ Purchase failed\nBTC · 50.00 USDT\nReason: 43012: Insufficient balance", key="buy_failed:43012")
    assert tg.texts()[-1].endswith("(+1 identical in the previous minute)")


async def test_force_bypasses_collapsing_but_not_the_rate_limit(tg, monkeypatch):
    monkeypatch.setattr(settings, "telegram_max_per_minute", 2)
    assert (await telegram.send_detailed("t", "k", force=True)).ok
    assert (await telegram.send_detailed("t", "k", force=True)).ok
    assert (await telegram.send_detailed("t", "k", force=True)).error == "rate limited"


# --------------------------------------------------------------------------- message formats

def _event(**kw):
    from types import SimpleNamespace as NS
    base = dict(type="buy_executed", dry_run=False, error=None, attempt=1, max_attempts=3, exchange="bitget", extra={},
                user=NS(email="support@minastech.ca"),
                purchase=NS(crypto_symbol="XLM", amount_usd=2.0, purchase_type="autobuy",
                            wallet_address="GDQP2KPQGKIHYJGXNUIYOMHARUARCA7DJT5FO2FFOOKY3B2WSQHG4W37", blockchain_network="XLM"),
                tx=NS(id=uuid.UUID(int=7), amount_executed=9.53, cost_usd=1.9998705, fill_price=0.20985, withdrawal_id="wd-778899",
                      withdrawal_chain="XLM", raw_status="filled", status="success", tx_type="buy"))
    base.update(kw)
    return type("E", (), base)()


def test_message_formats_are_short_masked_and_marked():
    text, key = telegram.format_purchase_event(_event())
    assert text == ("✅ Purchase completed\nXLM · 2.00 USDT → 9.53 XLM @ 0.2099 USDT\n"
                    "su…@minastech.ca · → withdrawing to the wallet next")
    text, _ = telegram.format_purchase_event(_event(purchase=_event().purchase.__class__(crypto_symbol="BTC", amount_usd=50, purchase_type="dca", wallet_address=None, blockchain_network=None),
                                                    tx=_event().tx.__class__(id=1, amount_executed=0.000594, cost_usd=49.98360312, fill_price=84147.14, withdrawal_id=None, withdrawal_chain=None, raw_status="filled", status="success", tx_type="buy")))
    assert text == "✅ Purchase completed\nBTC · 49.98 USDT → 0.000594 BTC @ 84147.14 USDT\nsu…@minastech.ca · held on the exchange"
    text, key = telegram.format_purchase_event(_event(type="buy_failed", error="43012: Insufficient balance", attempt=2))
    assert text == "❌ Purchase failed\nXLM · 2.00 USDT · attempt 2/3\nsu…@minastech.ca\nReason: 43012: Insufficient balance"
    assert key == "buy_failed:43012: Insufficient balance"
    text, _ = telegram.format_purchase_event(_event(type="buy_failed", error="43012: Insufficient balance", attempt=3, extra={"stopped": True}))
    assert text.startswith("⛔ Purchase stopped\n") and text.endswith("Stopped after repeated failures — needs a human.")
    text, _ = telegram.format_purchase_event(_event(type="buy_needs_review", error="fill amount 0 for order ORD-12345678901234567890123456"))
    assert text.startswith("🔍 Purchase parked for review\nXLM · 2.00 USDT\nsu…@minastech.ca\nReason: fill amount 0 for order ORD-12…3456")
    assert text.endswith("Needs admin review (Admin → Review); not retried automatically.")
    text, _ = telegram.format_purchase_event(_event(type="withdrawal_sent", tx=_event().tx.__class__(id=2, amount_executed=9.53, cost_usd=None, fill_price=None, withdrawal_id="wd-778899", withdrawal_chain="XLM", raw_status="submitted", status="success", tx_type="withdrawal")))
    assert text == "📤 Withdrawal sent\n9.53 XLM → GDQP2K…4W37 (XLM)\nsu…@minastech.ca · id wd-778899"
    text, _ = telegram.format_purchase_event(_event(type="withdrawal_sent", dry_run=True, tx=_event().tx.__class__(id=2, amount_executed=9.53, cost_usd=None, fill_price=None, withdrawal_id="sim", withdrawal_chain="XLM", raw_status="simulated", status="success", tx_type="withdrawal")))
    assert text.startswith("[dry run] 📤 Withdrawal sent (simulated, nothing left the exchange)")
    text, _ = telegram.format_purchase_event(_event(type="withdrawal_failed", error="address not in whitelist"))
    assert "GDQP2K…4W37" in text and "Coins are still on the exchange" in text and "Reason: address not in whitelist" in text
    text, _ = telegram.format_purchase_event(_event(type="withdrawal_needs_info", error="travel rule data required"))
    assert text.startswith("⚠️ Withdrawal needs information")
    assert telegram.format_purchase_event(_event(type="something_else")) is None
    for e in ("buy_executed", "buy_failed", "buy_needs_review", "withdrawal_sent", "withdrawal_failed", "withdrawal_needs_info"):
        t, _ = telegram.format_purchase_event(_event(type=e, error="x"))
        assert "support@minastech.ca" not in t and "GDQP2KPQGKIHYJGXNUIYOMHARUARCA7DJT5FO2FFOOKY3B2WSQHG4W37" not in t
        assert len(t) < 400 and TOKEN not in t


# --------------------------------------------------------------------------- the executor is untouched by Telegram

async def _snapshot(db, purchase_id):
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == purchase_id)
                             .order_by(PurchaseTransaction.created_at))).scalars().all()
    from tests.test_withdrawal_ops import reload
    p = await reload(db, purchase_id)
    return {"purchase": (p.status, p.failure_count, p.next_execution is not None, p.last_error),
            "rows": [(t.tx_type, t.status, t.raw_status, t.amount_executed, t.cost_usd, t.fill_price, bool(t.order_id), bool(t.withdrawal_id)) for t in rows]}


@pytest.fixture
def only_telegram_subscriber(monkeypatch):
    monkeypatch.setattr(ev, "_subscribers", [ev._telegram])


@pytest.mark.parametrize("mode", ["disabled", "enabled", "failing"])
async def test_executor_outcome_is_identical_with_telegram_disabled_enabled_and_failing(db, user, binance_key, only_telegram_subscriber, monkeypatch, mode):
    fake = TG()
    if mode == "disabled":
        monkeypatch.setattr(settings, "telegram_bot_token", "")
        monkeypatch.setattr(settings, "telegram_chat_id", "")
    else:
        monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
        monkeypatch.setattr(settings, "telegram_chat_id", CHAT)
        monkeypatch.setattr(settings, "telegram_max_per_minute", 20)
    if mode == "failing":
        fake.raise_times = 99   # every attempt times out
    monkeypatch.setattr(telegram, "transport_factory", lambda: httpx.MockTransport(fake.handler))
    telegram.reset_state()
    # a successful autobuy: buy + withdrawal
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(), dry_run=True)
    assert counts["executed"] == 1 and counts["failed"] == 0
    snap = await _snapshot(db, p.id)
    assert snap["purchase"][0] == "active" and snap["purchase"][2] is True
    assert [r[:2] for r in snap["rows"]] == [("buy", "success"), ("withdrawal", "success")]
    assert snap["rows"][0][3:6] == (0.002, 100.0, 50_000.0)
    # a failing buy: same parking regardless of Telegram
    p2 = await make_purchase(db, user, binance_key, amount_usd=50, next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    assert counts["failed"] == 1
    snap2 = await _snapshot(db, p2.id)
    assert snap2["rows"] == [("buy", "failed", None, None, None, None, False, False)]
    assert snap2["purchase"][1] == 1
    expected_calls = {"disabled": 0, "enabled": 3, "failing": 3 * 2}[mode]   # buy, withdrawal, failure; failing = two attempts each
    assert len(fake.requests) == expected_calls
    if mode == "enabled":
        texts = fake.texts()
        assert texts[0].startswith("[dry run] ✅ Purchase completed\nBTC · 100.00 USDT → 0.002 BTC @ 50,000.00 USDT")
        assert texts[1].startswith("[dry run] 📤 Withdrawal sent (simulated")
        assert texts[2].startswith("[dry run] ❌ Purchase failed\nBTC · 50.00 USDT · attempt 1/3")
        assert user.email not in "".join(texts) and p.wallet_address not in "".join(texts)


async def test_withdrawal_confirmed_by_the_poller_is_reported_after_the_commit(db, user, bitget_key, tg, monkeypatch):
    from api.services import purchase_events
    monkeypatch.setattr(purchase_events, "emit", _no_emit)
    p, buy, wd = await sent_withdrawal(db, user, bitget_key)
    counts = await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=30), gateway=FakeGateway(withdrawal_state="success"))
    assert counts["confirmed"] == 1
    (text,) = tg.texts()
    assert text == f"[dry run] ✅ Withdrawal confirmed\n0.001 BTC → {telegram.mask_address(p.wallet_address)} (BTC)\n{telegram.mask_email(user.email)} · tx 0xconfirmed"
    assert p.wallet_address not in text and user.email not in text


async def test_poller_outcome_identical_when_telegram_fails(db, user, bitget_key, tg, monkeypatch):
    from api.services import purchase_events
    monkeypatch.setattr(purchase_events, "emit", _no_emit)
    tg.raise_times = 99
    p, buy, wd = await sent_withdrawal(db, user, bitget_key)
    counts = await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=30), gateway=FakeGateway(withdrawal_state="success"))
    await db.refresh(wd)
    assert counts["confirmed"] == 1 and wd.tx_hash == "0xconfirmed" and wd.raw_status == "success"
    assert len(tg.requests) == 2, "one attempt plus one retry, then it gave up quietly"


async def _no_emit(db, event):
    return None
