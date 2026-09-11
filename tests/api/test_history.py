"""The history read model over purchase_transactions: listing, status vocabulary,
aggregates, holdings, timeline, digest inputs."""
from datetime import datetime, timedelta, timezone

import pytest

from api.services import history
from tests.api.conftest import make_autobuy_purchase, make_history


@pytest.fixture
async def rows(db, user, binance_key):
    btc = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    eth = await make_autobuy_purchase(db, user, crypto="ETH", amount_usd=150.0, purchase_type="dca", key=binance_key)
    a = await make_history(db, user, purchase=btc, amount_crypto=0.001, cost_usd=100.0,
                           fee=0.0001, fee_currency="BTC")
    b = await make_history(db, user, purchase=btc, amount_crypto=0.002, cost_usd=200.0)
    c = await make_history(db, user, purchase=eth, amount_crypto=0.05, cost_usd=150.0, status="failed",
                           error_message="boom")
    d = await make_history(db, user, purchase=eth, amount_crypto=0.05, cost_usd=150.0, status="pending")
    return {"btc": btc, "eth": eth, "a": a, "b": b, "c": c, "d": d}


def test_status_vocabulary_round_trips():
    assert history.to_api_status("success") == "completed"
    assert history.to_api_status("failed") == "failed"
    assert history.from_api_status("completed") == "success"
    assert history.from_api_status("pending") == "pending"


async def test_list_rows_joins_purchase_and_exchange(db, user, rows):
    items, total = await history.list_rows(db, user.id)
    assert total == 4 and len(items) == 4
    first = next(r for r in items if r.id == rows["a"].id)
    assert first.crypto == "BTC" and first.exchange == "binance" and first.purchase_type == "autobuy"
    assert first.tx_type == "buy" and first.status == "completed"
    assert first.wallet_address is None and first.blockchain_network is None, "a buy row carries no delivery"
    assert first.order_id.startswith("ord-") and first.withdrawal_id is None
    assert first.fee == 0.0001 and first.fee_currency == "BTC"
    assert first.exchange_rate == pytest.approx(100_000.0)
    failed = next(r for r in items if r.id == rows["c"].id)
    assert failed.tx_type == "buy" and failed.wallet_address is None
    assert failed.status == "failed" and failed.error_message == "boom"
    assert failed.amount_crypto == 0.0 and failed.cost_usd == 150.0


async def test_withdrawal_rows_carry_delivery_and_are_not_purchases(db, user, binance_key):
    btc = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    buy = await make_history(db, user, purchase=btc, amount_crypto=0.001, cost_usd=100.0, with_withdrawal=True,
                             withdrawal_id="WD-77", withdrawal_fee=0.00005, withdrawal_fee_currency="BTC",
                             tx_hash="0xabc", withdrawal_chain="BTC")
    items, total = await history.list_rows(db, user.id)
    assert total == 2
    wd = next(r for r in items if r.id == buy.withdrawal.id)
    assert wd.tx_type == "withdrawal" and wd.status == "completed" and wd.cost_usd == 0.0
    assert wd.wallet_address == btc.wallet_address and wd.blockchain_network == "BTC"
    assert wd.withdrawal_id == "WD-77" and wd.withdrawal_fee == 0.00005 and wd.tx_hash == "0xabc"
    # aggregates count the buy only
    assert (await history.stats(db, user.id))["total_invested_usdt"] == 100.0
    assert (await history.stats(db, user.id))["completed_count"] == 1
    assert await history.holdings(db, user.id) == [("BTC", pytest.approx(0.001), 100.0)]
    assert [r.id for r in await history.completed_rows(db, user.id)] == [buy.id]
    assert (await history.withdrawals_by_parent(db, user.id, [buy.id]))[buy.id].withdrawal_id == "WD-77"


async def test_filters_and_pagination(db, user, rows):
    done, total = await history.list_rows(db, user.id, status="completed")
    assert total == 2 and {r.id for r in done} == {rows["a"].id, rows["b"].id}
    eth, total = await history.list_rows(db, user.id, crypto="eth")
    assert total == 2 and all(r.crypto == "ETH" for r in eth)
    page, total = await history.list_rows(db, user.id, limit=1, offset=1)
    assert total == 4 and len(page) == 1
    assert await history.get_row(db, user.id, rows["a"].id) is not None
    assert await history.get_row(db, rows["btc"].user_id, rows["a"].id) is not None


async def test_stats_holdings_monthly_average(db, user, rows):
    st = await history.stats(db, user.id)
    assert st == {"total_transactions": 4, "total_invested_usdt": 300.0, "average_purchase_usdt": 150.0,
                  "completed_count": 2, "failed_count": 1, "pending_count": 1}
    assert await history.holdings(db, user.id) == [("BTC", pytest.approx(0.003), 300.0)]
    assert await history.monthly_average(db, user.id) == 300.0
    assert await history.completed_count_and_total(db, user.id) == (2, 300.0)


async def test_timeline_zero_fills(db, user, rows):
    pts = await history.timeline(db, user.id, 5)
    assert len(pts) == 5 and pts[-1]["total_usdt"] == 300.0 and pts[-1]["transaction_count"] == 2
    assert all(p["total_usdt"] == 0.0 for p in pts[:-1])


async def test_completed_rows_window_and_grouping(db, user, rows):
    now = datetime.now(timezone.utc)
    all_rows = await history.completed_rows(db, user.id)
    assert [r.id for r in all_rows] == [rows["a"].id, rows["b"].id]  # oldest first
    assert await history.completed_rows(db, user.id, since=now + timedelta(days=1)) == []
    assert history.group_by_crypto(all_rows) == [("BTC", pytest.approx(0.003), 300.0)]


async def test_failed_count_and_active_users(db, user, rows):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert await history.count_failed_since(db, since, user_id=user.id) == 1
    assert await history.count_failed_since(db, since) >= 1
    assert user.id in set(await history.users_with_activity_since(db, since))


async def test_legacy_rows_without_fill_details_still_read(db, user, binance_key):
    """Rows written before migration 013 carry only exchange_tx_id and the purchase amount."""
    p = await make_autobuy_purchase(db, user, crypto="SOL", amount_usd=40.0, purchase_type="dca", key=binance_key)
    tx = await make_history(db, user, purchase=p, amount_crypto=0.5, cost_usd=None, order_id=None,
                            fill_price=None, exchange_tx_id="ord-legacy", tx_type="accumulation")
    row = await history.get_row(db, user.id, tx.id)
    assert row.cost_usd == 40.0 and row.order_id == "ord-legacy" and row.exchange_rate == pytest.approx(80.0)
    assert row.tx_type == "accumulation" and row.status == "completed"
