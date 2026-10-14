"""The Auto-Buy executor records fill details and emits events; the subscribers
(purchase email, campaigns, dispatcher, Slack, webhooks) hang off those events."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.models import PurchaseTransaction, WebhookEndpoint
from api.services import purchase_events as ev
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.test_purchase_scheduler import NOW


from tests.fake_gateway import BuyFails, FakeGateway, RichGateway  # noqa: F401


@pytest.fixture
def captured(monkeypatch):
    """Replace every subscriber with a recorder."""
    seen = []

    async def record(db, event):
        seen.append(event)
    monkeypatch.setattr(ev, "_subscribers", [record])
    return seen


async def test_autobuy_success_records_fill_and_emits_buy_then_withdrawal(db, user, binance_key, captured):
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(), dry_run=True)
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id)
                             .order_by(PurchaseTransaction.created_at))).scalars().all()
    buy, wd = rows
    assert buy.status == "success" and buy.order_id.startswith("ORD-1-") and buy.fill_price == 50_000.0 and buy.cost_usd == 100
    assert wd.tx_type == "withdrawal" and wd.withdrawal_id.startswith("WD-1-") and wd.withdrawal_fee == 0.0001
    assert wd.withdrawal_fee_currency == "BTC" and wd.parent_transaction_id == buy.id
    assert [e.type for e in captured] == ["buy_executed", "withdrawal_sent"]
    assert captured[0].tx.id == buy.id and captured[1].tx.id == wd.id
    assert captured[0].exchange == "binance" and captured[0].dry_run is True
    payload = captured[1].payload()
    assert payload["wallet_address"] == p.wallet_address and payload["withdrawal_id"].startswith("WD-1-")


async def test_dca_success_emits_buy_only(db, user, binance_key, captured):
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(), dry_run=True)
    assert [e.type for e in captured] == ["buy_executed"]
    assert captured[0].payload()["tx_type"] == "buy" and captured[0].payload()["wallet_address"] is None


async def test_withdrawal_failure_keeps_fill_parks_purchase_and_never_rebuys(db, user, binance_key, captured):
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    gw = RichGateway(fail_withdrawal=True)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id)
                             .order_by(PurchaseTransaction.created_at))).scalars().all()
    buy, wd = rows
    assert buy.status == "success" and buy.order_id.startswith("ORD-1-"), "the buy that filled is recorded"
    assert wd.status == "failed" and "exchange said no" in wd.error_message
    await db.refresh(p)
    assert p.status == "withdrawal_failed" and p.failure_count == 0 and p.next_execution is None
    assert [e.type for e in captured] == ["buy_executed", "withdrawal_failed"]
    assert captured[1].error and captured[1].tx.id == wd.id
    # a later run never buys again for this purchase
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=10), gateway=gw, dry_run=True)
    assert len(gw.placed) == 1


async def test_buy_failure_emits_buy_failed(db, user, binance_key, captured):
    await make_purchase(db, user, binance_key, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    assert [e.type for e in captured] == ["buy_failed"]
    assert "MIN_NOTIONAL" in captured[0].error and captured[0].tx.amount_executed is None
    assert captured[0].tx.client_oid, "the attempt row carries the client id even when nothing was sent"


async def test_subscriber_errors_never_break_execution(db, user, binance_key, monkeypatch, caplog):
    async def explode(db, event):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(ev, "_subscribers", [explode])
    p = await make_purchase(db, user, binance_key, next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(), dry_run=True)
    assert counts["executed"] == 1
    await db.refresh(p)
    assert p.failure_count == 0 and p.status == "active"
    assert any("subscriber" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)


async def test_unknown_event_type_is_rejected(db, user, binance_key):
    p = await make_purchase(db, user, binance_key, next_execution=NOW)
    tx = PurchaseTransaction(purchase_id=p.id, status="pending")
    with pytest.raises(ValueError):
        await ev.emit(db, ev.PurchaseEvent(type="something_else", user=user, purchase=p, tx=tx))


# --------------------------------------------------------------------------- real subscribers

async def test_real_subscribers_email_campaign_webhook_slack(db, user, binance_key, monkeypatch):
    """With the real subscriber list: one purchase email, the first-purchase
    campaign email, a dispatcher audit row, a Slack call on failure, and a
    signed webhook per event."""
    from api.services import email_campaigns as camp, email_service as es_mod, purchase_events, slack_service
    from api.services import webhook_service
    from tests.api.test_email_service import CapturingEmail
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    monkeypatch.setattr(camp, "email_service", capture)
    slack_calls = []

    async def fake_slack(text):
        slack_calls.append(text)
        return True
    monkeypatch.setattr(slack_service, "send_slack", fake_slack)
    delivered = []

    async def fake_deliver(endpoint, payload):
        delivered.append(payload)
        return "200"
    monkeypatch.setattr(webhook_service, "deliver_one", fake_deliver)
    db.add(WebhookEndpoint(user_id=user.id, url="https://hooks.example.com/x", secret="s3cret", enabled=True))
    await db.commit()

    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(), dry_run=True)

    purchase_mails = [m for m in capture.sent if m["template"] == "purchase"]
    assert len(purchase_mails) == 1, "autobuy: one email per attempt, on the withdrawal outcome"
    # dry run: the withdrawal is simulated and the email must say so, never "sent to your wallet"
    assert purchase_mails[0]["subject"].startswith("[DRY RUN] ✓ Purchase complete") and "withdrawal simulated" in purchase_mails[0]["subject"]
    assert "sent to your wallet" not in purchase_mails[0]["subject"]
    assert p.wallet_address in purchase_mails[0]["text"] and "ORD-1" in purchase_mails[0]["text"]
    assert any(m["template"] == "first_purchase" for m in capture.sent)
    assert [d["event"] for d in delivered] == ["purchase.buy_executed", "purchase.withdrawal_sent"]
    assert delivered[0]["data"]["order_id"].startswith("ORD-1-") and delivered[1]["data"]["withdrawal_id"].startswith("WD-1-")
    assert delivered[0]["data"]["tx_type"] == "buy" and delivered[1]["data"]["tx_type"] == "withdrawal"
    assert slack_calls == [], "dry-run withdrawals do not page Slack"

    # a failing second purchase: Slack gets one run-summary line, the webhook fires per attempt,
    # and the customer is emailed once the cycle is exhausted (a one-time purchase: after 3 attempts)
    await make_purchase(db, user, binance_key, frequency="one_time", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    assert any("1 purchase(s) failed a buy attempt" in s for s in slack_calls)
    assert not any(m["subject"].startswith("[DRY RUN] ✗ Purchase") or "stopped" in m["subject"] for m in capture.sent), \
        "no email on attempt 1"
    assert delivered[-1]["event"] == "purchase.buy_failed" and delivered[-1]["data"]["error"]
    for _ in range(2):
        await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    assert any("We've paused your" in m["subject"] for m in capture.sent), "the stop is the one email it gets"
    assert any("PURCHASE STOPPED" in s for s in slack_calls)
    assert purchase_events.emit is not None


async def test_weekly_digest_job_runs_once_across_workers(db, user, binance_key, monkeypatch):
    import asyncio
    from api.services import email_campaigns as camp
    calls = {"n": 0}

    async def fake_summaries():
        calls["n"] += 1
        await asyncio.sleep(0.8)   # hold the lock long enough for the other workers to collide with it
        return 0
    monkeypatch.setattr(camp, "send_weekly_summaries", fake_summaries)
    results = await asyncio.gather(*[asyncio.to_thread(ps.send_weekly_digest) for _ in range(3)])
    assert sum(1 for r in results if r) == 1 and calls["n"] == 1, "one worker sends, the others find the lock held"


def test_build_scheduler_has_the_weekly_digest_job():
    sched = ps.build_scheduler()
    job = sched.get_job("weekly_digest")
    assert job is not None and job.func is ps.send_weekly_digest
    assert "day_of_week='mon'" in str(job.trigger) and "hour='9'" in str(job.trigger)
