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


class RichGateway:
    """What ExchangeClientGateway returns after a buy (+ withdrawal for autobuy)."""

    def __init__(self, fail_withdrawal=False):
        self.fail_withdrawal = fail_withdrawal
        self.n = 0   # ids are ORD-1/WD-1 for the first fill, ORD-2/WD-2 for the next... (exchange_tx_id is unique)

    def execute(self, account, purchase, dry_run):
        self.n += 1
        oid, wid = f"ORD-{self.n}", f"WD-{self.n}"
        fill = {"order_id": oid, "amount_executed": purchase.amount_usd / 50_000, "fill_price": 50_000.0,
                "cost_usd": purchase.amount_usd, "fee": None, "fee_currency": None}
        if purchase.purchase_type == "dca":
            return {**fill, "exchange_tx_id": oid, "tx_type": "accumulation", "withdrawal_id": None,
                    "withdrawal_fee": None, "withdrawal_fee_currency": None, "tx_hash": None}
        if self.fail_withdrawal:
            raise ps.ExecutionError("WITHDRAW_UNSUPPORTED: exchange said no", stage="withdraw", fill=fill)
        return {**fill, "exchange_tx_id": wid, "tx_type": "withdrawal", "withdrawal_id": wid,
                "withdrawal_fee": 0.0001, "withdrawal_fee_currency": "BTC", "tx_hash": "0xhash"}


class BuyFails:
    def execute(self, account, purchase, dry_run):
        raise ps.ExecutionError("MIN_NOTIONAL: too small")


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
    tx = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalar_one()
    assert tx.status == "success" and tx.order_id == "ORD-1" and tx.fill_price == 50_000.0 and tx.cost_usd == 100
    assert tx.withdrawal_id == "WD-1" and tx.withdrawal_fee == 0.0001 and tx.withdrawal_fee_currency == "BTC"
    assert tx.tx_hash == "0xhash" and tx.tx_type == "withdrawal"
    assert [e.type for e in captured] == ["buy_executed", "withdrawal_sent"]
    assert captured[0].exchange == "binance" and captured[0].dry_run is True and captured[0].tx.id == tx.id
    payload = captured[1].payload()
    assert payload["wallet_address"] == p.wallet_address and payload["withdrawal_id"] == "WD-1"


async def test_dca_success_emits_buy_only(db, user, binance_key, captured):
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(), dry_run=True)
    assert [e.type for e in captured] == ["buy_executed"]
    assert captured[0].payload()["tx_type"] == "accumulation" and captured[0].payload()["wallet_address"] is None


async def test_withdrawal_failure_keeps_fill_and_emits_withdrawal_failed(db, user, binance_key, captured):
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=RichGateway(fail_withdrawal=True), dry_run=True)
    tx = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalar_one()
    assert tx.status == "failed" and "exchange said no" in tx.error_message
    assert tx.order_id == "ORD-1" and tx.amount_executed == pytest.approx(0.002), "the buy that filled is recorded"
    await db.refresh(p)
    assert p.failure_count == 1
    assert [e.type for e in captured] == ["buy_executed", "withdrawal_failed"]
    assert captured[1].error and captured[1].attempt == 1 and captured[1].max_attempts == ps.MAX_ATTEMPTS


async def test_buy_failure_emits_buy_failed(db, user, binance_key, captured):
    await make_purchase(db, user, binance_key, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    assert [e.type for e in captured] == ["buy_failed"]
    assert "MIN_NOTIONAL" in captured[0].error and captured[0].tx.amount_executed is None


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
    assert purchase_mails[0]["subject"].startswith("[DRY RUN] ✓ Purchase complete") and "sent to your wallet" in purchase_mails[0]["subject"]
    assert p.wallet_address in purchase_mails[0]["text"] and "ORD-1" in purchase_mails[0]["text"]
    assert any(m["template"] == "first_purchase" for m in capture.sent)
    assert [d["event"] for d in delivered] == ["purchase.buy_executed", "purchase.withdrawal_sent"]
    assert delivered[0]["data"]["order_id"] == "ORD-1" and delivered[1]["data"]["withdrawal_id"] == "WD-1"
    assert slack_calls == [], "dry-run withdrawals do not page Slack"

    # a failing second purchase alerts Slack and emails the failure
    await make_purchase(db, user, binance_key, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    assert any("buy failed" in s for s in slack_calls)
    assert any(m["subject"].startswith("[DRY RUN] ✗ Purchase failed") for m in capture.sent)
    assert delivered[-1]["event"] == "purchase.buy_failed" and delivered[-1]["data"]["error"]
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
