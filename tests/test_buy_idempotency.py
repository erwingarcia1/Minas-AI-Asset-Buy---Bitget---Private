"""Priority 0: a buy is never placed twice.

- the buy row and its client_oid exist before the order is sent
- the fill is read from order detail, not from the place-order response
- an error after sending is reconciled by client_oid: a fill is recorded and
  never re-bought; not-found means a real failure; anything unclear parks the
  purchase as needs_review and pages Slack, with no retry
- before any retry, unresolved earlier attempts are reconciled first
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.models import PurchaseTransaction
from api.services import purchase_events as ev
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for


@pytest.fixture
def slack(monkeypatch):
    from api.services import slack_service
    calls = []

    async def fake(text):
        calls.append(text)
        return True
    monkeypatch.setattr(slack_service, "send_slack", fake)
    return calls


async def test_client_oid_is_stored_before_the_order_is_sent(db, user, binance_key):
    seen = {}

    class Peeking(FakeGateway):
        def place_buy(self, account, purchase, client_oid, dry_run):
            # the row must already be committed with this client id when the exchange is called
            import asyncio
            seen["client_oid"] = client_oid
            return super().place_buy(account, purchase, client_oid, dry_run)
    p = await make_purchase(db, user, binance_key, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=Peeking(), dry_run=True)
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.client_oid == seen["client_oid"]))).scalars().all()
    assert len(rows) == 1 and rows[0].purchase_id == p.id and rows[0].tx_type == "buy"
    assert rows[0].status == "success" and rows[0].order_id == "ORD-1"


async def test_place_order_returns_only_order_id_and_fill_comes_from_order_detail(db, user, binance_key):
    """Bitget's place-order answer carries only orderId; the fill (volumes, price, fee) is read from order detail."""
    class BitgetLike(FakeGateway):
        def place_buy(self, account, purchase, client_oid, dry_run):
            r = super().place_buy(account, purchase, client_oid, dry_run)
            return {"order_id": r["order_id"]}   # nothing else, like Bitget
    gw = BitgetLike()
    p = await make_purchase(db, user, binance_key, amount_usd=100, purchase_type="dca", next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert counts["executed"] == 1 and len(gw.placed) == 1, "one order, recorded as filled"
    (buy,) = await tx_for(db, p)
    assert buy.status == "success" and buy.amount_executed == pytest.approx(0.002)
    assert buy.fill_price == 50_000 and buy.cost_usd == 100 and buy.fee == pytest.approx(0.002 * 0.001)
    assert buy.fee_currency == "BTC" and buy.raw_status == "filled"


async def test_timeout_after_sending_is_reconciled_without_a_second_order(db, user, binance_key, caplog):
    gw = FakeGateway(place_raises_after_sending=True)
    p = await make_purchase(db, user, binance_key, amount_usd=100, purchase_type="dca", next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert counts["executed"] == 1
    assert len(gw.placed) == 1 and gw.lookups == [gw.placed[0][0]], "looked up by client id, not re-sent"
    (buy,) = await tx_for(db, p)
    assert buy.status == "success" and buy.order_id == "ORD-1"
    await db.refresh(p)
    assert p.failure_count == 0 and p.next_execution > NOW
    assert any("had filled; recording, not re-buying" in r.getMessage() for r in caplog.records)
    # the next run has nothing to do for it
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5), gateway=gw, dry_run=False)
    assert len(gw.placed) == 1


async def test_error_before_sending_is_a_plain_failure(db, user, binance_key):
    gw = FakeGateway(fail_buy=True)
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "failed" and gw.placed == []
    await db.refresh(p)
    assert p.failure_count == 1 and p.status == "active" and p.next_execution == NOW, "retried next run"


async def test_fill_unavailable_parks_as_needs_review_and_pages_slack(db, user, binance_key, slack):
    gw = FakeGateway(fill_unavailable=True)
    p = await make_purchase(db, user, binance_key, amount_usd=100, purchase_type="dca", next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert counts["failed"] == 1 and len(gw.placed) == 1
    (buy,) = await tx_for(db, p)
    assert buy.status == "needs_review" and "fill could not be read" in buy.error_message
    await db.refresh(p)
    assert p.status == "needs_review" and p.next_execution is None and p.failure_count == 0
    assert slack and "NEEDS REVIEW" in slack[0] and buy.client_oid in slack[0]
    # never retried: not due, and a later run places nothing
    assert p.id not in {x.id for x in await ps.find_due_purchases(db, NOW + timedelta(days=1))}
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5), gateway=gw, dry_run=False)
    assert len(gw.placed) == 1


async def test_send_error_with_unknown_state_is_needs_review(db, user, binance_key, slack):
    gw = FakeGateway(place_raises_after_sending=True, lookup_raises=True)
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "needs_review" and "could not be looked up" in buy.error_message
    await db.refresh(p)
    assert p.status == "needs_review" and slack


async def test_earlier_unresolved_attempt_is_reconciled_before_any_new_buy(db, user, binance_key, slack):
    """A crash between sending and recording leaves a pending row with a client id.
    The next run asks the exchange about it first."""
    p = await make_purchase(db, user, binance_key, amount_usd=100, purchase_type="dca", next_execution=NOW)
    stale = PurchaseTransaction(purchase_id=p.id, status="pending", tx_type="buy", client_oid="ab-stale-0001")
    db.add(stale)
    await db.commit()
    db.expire_all()

    # (a) it had filled -> recorded on the old row, no new order
    gw = FakeGateway(lookup_state="filled")
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert gw.placed == [] and gw.lookups == ["ab-stale-0001"]
    await db.refresh(p)
    await db.refresh(stale)
    assert stale.status == "success" and stale.amount_executed == pytest.approx(0.002)
    assert p.next_execution > NOW and p.failure_count == 0

    # (b) an unresolved one that is still open on the exchange -> needs_review, no new order
    p2 = await make_purchase(db, user, binance_key, amount_usd=100, purchase_type="dca", next_execution=NOW)
    stale2 = PurchaseTransaction(purchase_id=p2.id, status="pending", tx_type="buy", client_oid="ab-stale-0002")
    db.add(stale2)
    await db.commit()
    db.expire_all()
    gw = FakeGateway(lookup_state="open")
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert gw.placed == []
    await db.refresh(p2)        # refreshing the purchase cascades an expire onto its rows: refresh it first
    await db.refresh(stale2)
    assert stale2.status == "needs_review" and p2.status == "needs_review" and slack

    # (c) never reached the exchange -> old row failed, a new buy proceeds
    p3 = await make_purchase(db, user, binance_key, amount_usd=100, purchase_type="dca", next_execution=NOW)
    stale3 = PurchaseTransaction(purchase_id=p3.id, status="pending", tx_type="buy", client_oid="ab-stale-0003")
    db.add(stale3)
    await db.commit()
    db.expire_all()
    gw = FakeGateway(lookup_state="not_found")
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert counts["executed"] == 1 and len(gw.placed) == 1
    await db.refresh(p3)
    await db.refresh(stale3)
    assert stale3.status == "failed" and "not_found" in stale3.error_message
    rows = await tx_for(db, p3)
    assert [t.status for t in rows] == ["failed", "success"]


async def test_needs_info_withdrawal_parks_purchase_and_emails_user(db, user, binance_key, monkeypatch):
    from api.services import email_service as es_mod
    from tests.api.test_email_service import CapturingEmail
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    gw = FakeGateway(withdrawal_needs_info=True)
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    buy, wd = await tx_for(db, p)
    assert buy.status == "success" and wd.status == "needs_info" and "travel rule" in wd.error_message
    await db.refresh(p)
    assert p.status == "needs_info" and p.next_execution is None
    mail = [m for m in capture.sent if m["template"] == "purchase"]
    assert mail and "needs more information" in mail[0]["subject"] and "travel rule" in mail[0]["text"]
    assert "will NOT" in mail[0]["text"] or "nothing will be retried" in mail[0]["text"]
    # no automatic retry of anything
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=10), gateway=gw, dry_run=True)
    assert len(gw.placed) == 1 and len(gw.withdrawals) == 1


async def test_events_use_the_right_rows(db, user, binance_key, monkeypatch):
    seen = []

    async def record(db_, event):
        seen.append(event)
    monkeypatch.setattr(ev, "_subscribers", [record])
    p = await make_purchase(db, user, binance_key, amount_usd=100, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    buy, wd = await tx_for(db, p)
    assert [(e.type, e.tx.id) for e in seen] == [("buy_executed", buy.id), ("withdrawal_sent", wd.id)]
