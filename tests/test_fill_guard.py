"""A buy is recorded as completed only for a sane fill. Zero, partial,
implausible and unfilled orders are parked as needs_review with the Slack
alert, never advance the schedule, are never retried into a second buy, and
never reach the withdrawal gateway. Failed attempts spend nothing and count
for nothing. All of this runs against the fake gateway (unit level)."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.config import settings
from api.models import PurchaseTransaction
from api.services import history
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import BuyFails, FakeGateway
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


async def reload(db, purchase):
    from sqlalchemy.orm import selectinload
    from api.models import Purchase
    return (await db.execute(select(Purchase).options(selectinload(Purchase.transactions), selectinload(Purchase.user),
                                                      selectinload(Purchase.exchange_account))
                             .execution_options(populate_existing=True).where(Purchase.id == purchase.id))).scalar_one()


# --------------------------------------------------------------------------- validate_fill

def test_validate_fill_accepts_normal_market_fills_and_rejects_zero_partial_and_implausible(monkeypatch):
    monkeypatch.setattr(settings, "fill_tolerance_pct", 10)
    good = {"filled": True, "raw_status": "filled", "amount_crypto": 0.00019, "exchange_rate": 78_950.0, "cost_usdt": 15.0}
    assert ps.validate_fill(good, 15.0, reference_price=78_900.0) is None
    # taker fee in base coin + 1% slippage: still fine
    assert ps.validate_fill({**good, "amount_crypto": 0.00019 * 0.988}, 15.0, 78_900.0) is None
    assert "not filled" in ps.validate_fill({**good, "filled": False, "raw_status": "live"}, 15.0)
    assert "amount 0" in ps.validate_fill({**good, "amount_crypto": 0}, 15.0)
    assert "cost 0" in ps.validate_fill({**good, "cost_usdt": 0}, 15.0)
    assert "partial fill" in ps.validate_fill({**good, "cost_usdt": 7.0, "amount_crypto": 0.0000887}, 15.0)
    assert "outside ±10%" in ps.validate_fill({**good, "amount_crypto": 0.00019 * 0.5}, 15.0, 78_900.0)
    assert "outside ±10%" in ps.validate_fill({**good, "amount_crypto": 0.00019 * 1.2}, 15.0, 78_900.0)
    assert "inconsistent" in ps.validate_fill({**good, "exchange_rate": 40_000.0}, 15.0, 78_900.0)   # amount matches the reference, the reported average does not
    # without a reference the fill's own average is the yardstick; a consistent fill passes
    assert ps.validate_fill(good, 15.0) is None
    # the tolerance is configurable
    monkeypatch.setattr(settings, "fill_tolerance_pct", 1)
    assert "outside ±1%" in ps.validate_fill({**good, "amount_crypto": 0.00019 * 0.97}, 15.0, 78_900.0)


# --------------------------------------------------------------------------- executor paths

@pytest.mark.parametrize("gateway_kwargs, expect", [
    ({"fill_factor": 0.0}, "amount 0"),                                  # filled status, zero volume
    ({"fill_factor": 0.5}, "outside ±10%"),                              # half the coins for the full cost
    ({"fill_override": {"cost_usdt": 0.0}}, "cost 0"),                   # cost missing
    ({"fill_override": {"cost_usdt": 6.0, "amount_crypto": 0.00012}}, "partial fill"),
    ({"unfilled": True}, "not filled"),                                  # order detail says live
])
async def test_bad_fills_park_the_purchase_for_review_and_never_advance(db, user, bitget_key, slack, gateway_kwargs, expect, monkeypatch):
    monkeypatch.setattr(settings, "fill_tolerance_pct", 10)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=50, frequency="weekly", next_execution=NOW)
    gw = FakeGateway(reference_price=50_000.0, **gateway_kwargs)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    assert counts["failed"] == 1
    p = await reload(db, p)
    (tx,) = await tx_for(db, p)
    assert tx.status == "needs_review" and expect in tx.error_message, tx.error_message
    assert tx.amount_executed is None and tx.cost_usd is None, "nothing is recorded as bought"
    assert p.status == "needs_review" and p.next_execution is None, "schedule not advanced, purchase parked"
    assert len(slack) == 1 and "NEEDS REVIEW" in slack[0]
    # the next run must not buy again: the purchase is no longer due and the row is not pending
    counts = await ps.execute_due_purchases(db, now=NOW + timedelta(days=1), gateway=gw, dry_run=False)
    assert counts == {"executed": 0, "failed": 0, "skipped": 0} and len(gw.placed) == 1
    # and it counts for nothing
    st = await history.stats(db, user.id)
    assert st["completed_count"] == 0 and st["total_invested_usdt"] == 0 and st["needs_review_count"] == 1
    assert await history.holdings(db, user.id) == []


async def test_bad_fill_found_during_reconciliation_is_also_parked(db, user, bitget_key, slack):
    """An earlier unresolved attempt that turns out 'filled' with zero volume is not recorded as a buy."""
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=50, next_execution=NOW)
    gw = FakeGateway(place_raises_after_sending=True, fill_factor=0.0)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    p = await reload(db, p)
    (tx,) = await tx_for(db, p)
    assert tx.status == "needs_review" and "implausible" in tx.error_message and "amount 0" in tx.error_message
    assert p.status == "needs_review" and len(gw.placed) == 1


async def test_good_fill_within_tolerance_is_recorded_and_withdrawn(db, user, bitget_key, slack):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, next_execution=NOW)
    gw = FakeGateway(reference_price=50_000.0, fill_factor=0.99)   # 1% slippage
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert counts["executed"] == 1 and slack == []
    buy, wd = await tx_for(db, p)
    assert buy.status == "success" and wd.status == "success" and len(gw.withdrawals) == 1


# --------------------------------------------------------------------------- withdrawal guard

async def test_withdrawal_is_never_attempted_on_an_unknown_or_zero_buy_amount(db, user, bitget_key, monkeypatch):
    """Belt and braces: even if a zero-amount 'success' buy existed, the withdrawal step refuses it."""
    from api.models import Purchase
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, next_execution=NOW)
    buy = PurchaseTransaction(purchase_id=p.id, tx_type="buy", status="success", client_oid="ab-zero", order_id="ORD-Z",
                              amount_executed=0.0, cost_usd=60.0, executed_at=NOW)
    db.add(buy)
    await db.commit()
    p = await reload(db, p)
    gw = FakeGateway()
    events = []
    wd = await ps._withdraw_after_buy(db, p, p.exchange_account, buy, gw, NOW, True, events)
    assert gw.withdrawals == [], "the gateway was never called"
    assert wd.status == "failed" and "withdrawal not attempted" in wd.error_message and "0.0" in wd.error_message
    await db.refresh(p)
    assert p.status == "withdrawal_failed" and p.next_execution is None
    assert [e[0] for e in events] == ["withdrawal_failed"]
    # retry-withdrawal hits the same guard: a second failed withdrawal row, still no exchange call
    p = await reload(db, p)
    assert ps.stranded_buy(p) is not None
    wd2 = await ps.retry_withdrawal(db, p, gateway=gw, now=NOW, dry_run=True)
    assert wd2.status == "failed" and "withdrawal not attempted" in wd2.error_message and gw.withdrawals == []
    await db.refresh(p)
    assert p.status == "withdrawal_failed"


# --------------------------------------------------------------------------- failed attempts

async def test_failed_attempt_never_advances_the_schedule_and_counts_for_nothing(db, user, bitget_key, client):
    from tests.conftest_autobuy import auth
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, frequency="daily", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    p = await reload(db, p)
    assert p.status == "active" and p.failure_count == 1 and p.next_execution == NOW, "still due: retried next run, not advanced"
    (tx,) = await tx_for(db, p)
    assert tx.status == "failed" and tx.amount_executed is None and tx.cost_usd is None
    st = await history.stats(db, user.id)
    assert st["completed_count"] == 0 and st["total_invested_usdt"] == 0 and st["average_purchase_usdt"] == 0 and st["failed_count"] == 1
    assert await history.holdings(db, user.id) == [] and await history.monthly_average(db, user.id) == 0
    assert await history.completed_count_and_total(db, user.id) == (0, 0.0)
    assert all(day["total_usdt"] == 0 for day in await history.timeline(db, user.id, 7, now=NOW + timedelta(hours=1)))
    rows = (await client.get("/api/transactions/", headers=auth(user))).json()["items"]
    assert rows[0]["status"] == "failed" and rows[0]["cost_usdt"] == 0.0 and rows[0]["amount_crypto"] == 0.0, \
        "a failed attempt carries no cost in the history"
    summary = (await client.get("/api/transactions/stats/summary", headers=auth(user))).json()
    assert summary["completed_count"] * summary["average_purchase_usdt"] == pytest.approx(summary["total_invested_usdt"])
    # a later success is what gets counted, and the tiles agree with each other
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5), gateway=FakeGateway(), dry_run=True)
    summary = (await client.get("/api/transactions/stats/summary", headers=auth(user))).json()
    assert summary["completed_count"] == 1 and summary["failed_count"] == 1 and summary["total_transactions"] == 2
    assert summary["completed_count"] * summary["average_purchase_usdt"] == pytest.approx(summary["total_invested_usdt"])


async def test_network_failures_are_counted_for_the_admin_alert(db, user, bitget_key):
    from datetime import datetime, timezone
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, next_execution=NOW)
    for msg in ("NETWORK: Bitget error NETWORK: GET tickers: ConnectionError: Temporary failure in name resolution", "MIN_NOTIONAL: too small"):
        db.add(PurchaseTransaction(purchase_id=p.id, tx_type="buy", status="failed", client_oid=f"ab-{msg[:4]}-{p.id.hex[:6]}",
                                   error_message=msg, executed_at=datetime.now(timezone.utc)))
    await db.commit()
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert await history.count_network_failures_since(db, since) == 1
    assert await history.count_failed_since(db, since, user_id=user.id) == 2
