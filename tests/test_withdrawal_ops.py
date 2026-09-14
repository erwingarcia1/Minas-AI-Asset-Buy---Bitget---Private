"""Withdrawal follow-up: the poller that confirms sent withdrawals with the
exchange, and the retry that re-sends a stranded buy's coins without ever
buying again."""
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.models import Purchase, PurchaseTransaction
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import (BTC_MAIN, auth, binance_key, bitget_key, client, db, make_key, make_purchase,  # noqa: F401
                                    other_user, user)
from tests.fake_gateway import FailingGateway, FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for


@pytest.fixture
def events(monkeypatch):
    """Captured purchase events instead of the real subscribers (email/Slack)."""
    from api.services import purchase_events
    seen = []

    async def fake_emit(db, event):
        seen.append(event)
    monkeypatch.setattr(purchase_events, "emit", fake_emit)
    return seen


async def reload(db, purchase_id):
    return (await db.execute(select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.user),
                                                      selectinload(Purchase.exchange_account))
                             .execution_options(populate_existing=True)   # refresh collections already in the session
                             .where(Purchase.id == purchase_id))).scalar_one()


async def sent_withdrawal(db, user, key, raw_status="submitted", executed_at=NOW, tx_hash=None, **purchase_fields):
    """A filled buy plus its sent withdrawal row, as the executor leaves them."""
    p = await make_purchase(db, user, key, purchase_type="autobuy", **purchase_fields)
    buy = PurchaseTransaction(purchase_id=p.id, tx_type="buy", status="success", client_oid=f"ab-{uuid.uuid4().hex[:24]}",
                              order_id="ORD-x", exchange_tx_id=f"ORD-{uuid.uuid4().hex[:6]}", amount_executed=0.001,
                              fill_price=50_000.0, cost_usd=50.0, executed_at=executed_at)
    db.add(buy)
    await db.commit()
    wd = PurchaseTransaction(purchase_id=p.id, tx_type="withdrawal", status="success", parent_transaction_id=buy.id,
                             client_oid=f"abw-{uuid.uuid4().hex[:24]}", withdrawal_id=f"WD-{uuid.uuid4().hex[:6]}",
                             exchange_tx_id=f"WD-{uuid.uuid4().hex[:6]}", amount_executed=0.001, withdrawal_chain="BTC",
                             raw_status=raw_status, tx_hash=tx_hash, executed_at=executed_at)
    db.add(wd)
    await db.commit()
    return p, buy, wd


# --------------------------------------------------------------------------- poller

async def test_poller_stores_tx_hash_and_final_fee_on_success(db, user, bitget_key, events):
    p, buy, wd = await sent_withdrawal(db, user, bitget_key)
    gw = FakeGateway(withdrawal_state="success")
    counts = await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=30), gateway=gw)
    await db.refresh(wd)
    assert counts["confirmed"] == 1 and gw.status_calls == [(wd.client_oid, wd.withdrawal_id)]
    assert wd.status == "success" and wd.raw_status == "success" and wd.tx_hash == "0xconfirmed"
    assert wd.withdrawal_fee == pytest.approx(0.00003)
    assert events == []
    # settled rows are not polled again
    assert await ps.find_open_withdrawals(db, NOW + timedelta(hours=1)) == []


async def test_poller_parks_purchase_when_exchange_rejects_the_withdrawal(db, user, bitget_key, events):
    p, buy, wd = await sent_withdrawal(db, user, bitget_key)
    counts = await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=30), gateway=FakeGateway(withdrawal_state="wallet_fail"))
    await db.refresh(wd)
    await db.refresh(p)
    assert counts["failed"] == 1
    assert wd.status == "failed" and wd.raw_status == "wallet_fail" and "wallet_fail" in wd.error_message
    assert p.status == "withdrawal_failed" and p.next_execution is None
    assert [e.type for e in events] == ["withdrawal_failed"] and events[0].tx.id == wd.id
    assert buy.status == "success", "the buy is untouched; only the withdrawal may be retried"


async def test_poller_leaves_in_flight_and_unreachable_rows_pending(db, user, bitget_key, events):
    p, buy, wd = await sent_withdrawal(db, user, bitget_key)
    assert (await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=30), gateway=FakeGateway(withdrawal_state="wallet_processing")))["pending"] == 1
    await db.refresh(wd)
    assert wd.status == "success" and wd.raw_status == "wallet_processing" and wd.tx_hash is None
    assert (await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=40), gateway=FakeGateway(status_raises=True)))["pending"] == 1
    assert (await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=50), gateway=FakeGateway(withdrawal_state=None)))["pending"] == 1
    await db.refresh(p)
    assert p.status == "active" and events == []


async def test_poller_skips_dry_run_rows_and_other_exchanges(db, user, bitget_key, binance_key, events):
    await sent_withdrawal(db, user, bitget_key, raw_status="simulated")     # dry run: nothing was sent
    await sent_withdrawal(db, user, binance_key)                            # no tracking for other venues
    gw = FakeGateway(withdrawal_state="success")
    counts = await ps.poll_withdrawals(db, now=NOW + timedelta(minutes=30), gateway=gw)
    assert gw.status_calls == [] and counts == {"confirmed": 0, "failed": 0, "pending": 0, "skipped": 1}


async def test_poller_flags_withdrawals_unconfirmed_after_the_window(db, user, bitget_key, events, monkeypatch):
    from api.services import slack_service
    slack = []

    async def fake(text):
        slack.append(text)
        return True
    monkeypatch.setattr(slack_service, "send_slack", fake)
    old = NOW - ps.WITHDRAWAL_POLL_WINDOW + timedelta(minutes=30)
    p, buy, wd = await sent_withdrawal(db, user, bitget_key, executed_at=old)
    await ps.poll_withdrawals(db, now=NOW, gateway=FakeGateway(withdrawal_state=None))
    await db.refresh(wd)
    assert wd.raw_status == "unconfirmed" and wd.status == "success"
    assert len(slack) == 1 and str(wd.id) in slack[0]


# --------------------------------------------------------------------------- retry

async def test_retry_sends_only_the_withdrawal_and_resumes_the_schedule(db, user, bitget_key, events):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", frequency="weekly", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    del events[:]   # only the retry's own events matter below
    p = await reload(db, p.id)
    assert p.status == "withdrawal_failed" and p.next_execution is None
    buy = ps.stranded_buy(p)
    assert buy is not None and buy.tx_type == "buy"

    gw = FakeGateway()
    wd = await ps.retry_withdrawal(db, p, gateway=gw, now=NOW + timedelta(hours=1), dry_run=True)
    assert gw.placed == [], "retrying a withdrawal never places an order"
    assert len(gw.withdrawals) == 1 and gw.withdrawals[0][2] == pytest.approx(buy.amount_executed - buy.fee)
    assert wd.status == "success" and wd.parent_transaction_id == buy.id and wd.tx_type == "withdrawal"
    assert wd.order_id == buy.order_id and wd.raw_status == "simulated"
    await db.refresh(p)
    assert p.status == "active" and p.next_execution == NOW + timedelta(hours=1) + timedelta(days=7)
    assert [e.type for e in events] == ["withdrawal_sent"]
    rows = await tx_for(db, p)
    assert [r.tx_type for r in rows] == ["buy", "withdrawal", "withdrawal"]
    assert [r.status for r in rows] == ["success", "failed", "success"]
    assert ps.stranded_buy(await reload(db, p.id)) is None


async def test_retry_that_fails_again_keeps_the_purchase_parked(db, user, bitget_key, events):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    del events[:]
    p = await reload(db, p.id)
    wd = await ps.retry_withdrawal(db, p, gateway=FakeGateway(withdrawal_needs_info=True), now=NOW, dry_run=True)
    assert wd.status == "needs_info"
    await db.refresh(p)
    assert p.status == "needs_info" and p.next_execution is None and "travel rule" in p.last_error
    assert [e.type for e in events] == ["withdrawal_needs_info"]


async def test_retry_refuses_purchases_that_are_not_parked(db, user, bitget_key, events):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    p = await reload(db, p.id)
    assert p.status == "active" and ps.stranded_buy(p) is None
    with pytest.raises(ValueError, match="only withdrawal_failed"):
        await ps.retry_withdrawal(db, p, gateway=FakeGateway(), now=NOW, dry_run=True)


async def test_one_time_purchase_completes_after_a_successful_retry(db, user, bitget_key, events):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", frequency="one_time", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    p = await reload(db, p.id)
    await ps.retry_withdrawal(db, p, gateway=FakeGateway(), now=NOW, dry_run=True)
    await db.refresh(p)
    assert p.status == "completed" and p.next_execution is None


# --------------------------------------------------------------------------- API

async def test_retry_endpoint(db, user, other_user, bitget_key, client, events, monkeypatch):
    from api.routes import autobuy_purchases as routes
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60.0, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    gw = FakeGateway()
    monkeypatch.setattr(routes, "_gateway", lambda: gw)
    c = client
    other = await c.post(f"/api/v2/purchases/{p.id}/retry-withdrawal", headers=auth(other_user))
    assert other.status_code == 404
    r = await c.post(f"/api/v2/purchases/{p.id}/retry-withdrawal", headers=auth(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active" and body["next_execution"] is not None
    assert body["withdrawal"]["tx_type"] == "withdrawal" and body["withdrawal"]["status"] == "simulated"   # dry run
    assert body["withdrawal"]["withdrawal_chain"] == "BTC" and body["withdrawal"]["parent_transaction_id"]
    assert gw.placed == [] and len(gw.withdrawals) == 1
    again = await c.post(f"/api/v2/purchases/{p.id}/retry-withdrawal", headers=auth(user))
    assert again.status_code == 409 and again.json()["detail"]["error"] == "not_retryable"
    detail = (await c.get(f"/api/v2/purchases/{p.id}", headers=auth(user))).json()
    assert [t["tx_type"] for t in detail["transactions"]] == ["withdrawal", "withdrawal", "buy"]
    assert detail["transactions"][0]["tx_hash"] is None and detail["transactions"][2]["order_id"].startswith("ORD-1-")


async def test_retry_endpoint_rejects_when_bitget_data_says_no(db, user, bitget_key, client, events, monkeypatch):
    """The wallet/chain/minimum checks from creation run again before a retry."""
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=15.0, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    c = client
    r = await c.post(f"/api/v2/purchases/{p.id}/retry-withdrawal", headers=auth(user))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "below_minimum_withdrawal"


async def test_travel_rule_info_is_stored_optional_and_sent_with_bitget_withdrawals(db, user, bitget_key, client, monkeypatch):
    info = {"first_name": "Ada", "last_name": "Lovelace", "identity_type": "user", "area_code": "", "member_code": None}
    c = client
    r = await c.post("/api/v2/purchases", headers=auth(user), json={
        "crypto_symbol": "BTC", "amount_usd": 60, "frequency": "weekly", "wallet_address": BTC_MAIN,
        "blockchain_network": "BTC", "exchange_account_id": str(bitget_key.id), "purchase_type": "autobuy",
        "travel_rule_info": info})
    assert r.status_code == 201, r.text
    pid = r.json()["purchase_id"]
    got = (await c.get(f"/api/v2/purchases/{pid}", headers=auth(user))).json()
    assert got["travel_rule_info"] == {"first_name": "Ada", "last_name": "Lovelace", "identity_type": "user",
                                       "company_name": None, "area_code": None, "member_code": None}
    # optional: a purchase without it reports null, and {} clears it
    plain = await c.post("/api/v2/purchases", headers=auth(user), json={
        "crypto_symbol": "BTC", "amount_usd": 60, "frequency": "weekly", "exchange_account_id": str(bitget_key.id),
        "purchase_type": "dca"})
    assert plain.status_code == 201 and (await c.get(f"/api/v2/purchases/{plain.json()['purchase_id']}", headers=auth(user))).json()["travel_rule_info"] is None
    cleared = await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"travel_rule_info": {}})
    assert cleared.status_code == 200 and cleared.json()["travel_rule_info"] is None
    await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"travel_rule_info": info})
    p = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(pid)))).scalar_one()
    assert json.loads(p.travel_rule_info) == {"first_name": "Ada", "last_name": "Lovelace", "identity_type": "user"}

    # the Bitget client sends them under Bitget's names, only when present
    from bitget_client import BitgetClient
    sent = {}

    def fake_request(self, method, path, params=None, body=None, auth=True):
        sent.update(body)
        return {"orderId": "WD-77"}
    monkeypatch.setattr(BitgetClient, "_request", fake_request)
    bc = BitgetClient.__new__(BitgetClient)
    assert bc.withdraw("BTC", BTC_MAIN, 0.001, network="BTC", client_oid="abw-1", travel_rule=p.travel_rule_dict()) == "WD-77"
    assert sent["firstName"] == "Ada" and sent["lastName"] == "Lovelace" and sent["identityType"] == "user"
    assert "areaCode" not in sent and "memberCode" not in sent and sent["chain"] == "BTC" and sent["clientOid"] == "abw-1"
    sent.clear()
    bc.withdraw("BTC", BTC_MAIN, 0.001, network="BTC", client_oid="abw-2", travel_rule=None)
    assert not any(k in sent for k in ("firstName", "lastName", "identityType"))

    # the executor gateway forwards the stored details to the client
    class Recorder:
        exchange = "bitget"
        def withdraw(self, coin, address, amount, network=None, client_oid=None, dry_run=True, travel_rule=None):
            sent["travel_rule"] = travel_rule
            return {"withdrawal_id": "DRY", "amount": amount, "dry_run": dry_run}
    monkeypatch.setattr(ps, "_client_for", lambda account: Recorder())
    p = await reload(db, p.id)
    ps.ExchangeClientGateway().withdraw(bitget_key, p, 0.001, "abw-3", True)
    assert sent["travel_rule"] == {"first_name": "Ada", "last_name": "Lovelace", "identity_type": "user"}
