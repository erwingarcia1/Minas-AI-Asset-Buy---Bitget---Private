"""Holdings are net of the coin-denominated trading fee, and withdrawals are
subtracted per the fee mode each was SENT under (read from the row, never
from today's configuration).

The three buy rows are the live Bitget fills of 2026-09-16/21 on the
production server; the expected net quantities are the balances Bitget
reported for that account on 2026-09-21 (BTC 0.000196803, XLM 14.30568,
XRP 1.3320666), matched to the last digit.
"""
import pytest

from api.models import Purchase, PurchaseTransaction, utcnow
from api.services import history
from tests.conftest_autobuy import bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401

LIVE_BUYS = [  # coin, gross fill, fee (in the coin), fill price, cost USD
    ("BTC", 0.000197, 1.97e-07, 76132.62, 14.99812614),
    ("XLM", 14.32, 0.01432, 0.20945, 2.999324),
    ("XRP", 1.3334, 0.0013334, 1.4999, 1.99996666),
]
BITGET_BALANCES_2026_09_21 = {"BTC": 0.000196803, "XLM": 14.30568, "XRP": 1.3320666}


async def _seed(db, user, key):
    purchases = {}
    for coin, gross, fee, price, cost in LIVE_BUYS:
        p = await make_purchase(db, user, key, crypto_symbol=coin, purchase_type="dca", wallet_address=None,
                                blockchain_network=None, amount_usd=cost, status="completed", next_execution=None)
        db.add(PurchaseTransaction(purchase_id=p.id, tx_type="buy", status="success", amount_executed=gross, fee=fee,
                                   fee_currency=coin, fill_price=price, cost_usd=cost, raw_status="filled",
                                   order_id=f"live-{coin}", client_oid=f"ab-{coin}", executed_at=utcnow()))
        purchases[coin] = p
    await db.commit()
    return purchases


async def test_acquired_matches_the_live_bitget_balances(db, user, bitget_key):
    await _seed(db, user, bitget_key)
    by = {p.crypto: p for p in await history.positions(db, user.id)}
    for coin, expected in BITGET_BALANCES_2026_09_21.items():
        assert by[coin].acquired == pytest.approx(expected, abs=1e-12), coin
        assert by[coin].on_exchange == pytest.approx(expected, abs=1e-12), "nothing withdrawn yet: all on the exchange"
        assert by[coin].sent_to_wallet == 0.0 and by[coin].withdrawals == 0
    # the plain holdings tuple (P&L, average price, cost shares) carries the same net figure
    assert dict((c, a) for c, a, _ in await history.holdings(db, user.id))["BTC"] == pytest.approx(0.000196803, abs=1e-12)
    # ordered by cost, biggest first
    assert [p.crypto for p in await history.positions(db, user.id)] == ["BTC", "XLM", "XRP"]


async def test_a_fee_charged_in_usdt_does_not_touch_the_quantity(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", purchase_type="dca", wallet_address=None,
                            blockchain_network=None, status="completed", next_execution=None)
    db.add(PurchaseTransaction(purchase_id=p.id, tx_type="buy", status="success", amount_executed=2.0, fee=0.004,
                               fee_currency="USDT", fill_price=1.5, cost_usd=3.0, raw_status="filled", order_id="o", client_oid="c"))
    await db.commit()
    assert (await history.positions(db, user.id))[0].acquired == 2.0


def _withdrawal(purchase_id, parent_id, size, fee, expected_received, raw_status="success"):
    return PurchaseTransaction(purchase_id=purchase_id, parent_transaction_id=parent_id, tx_type="withdrawal", status="success",
                               amount_executed=size, withdrawal_fee=fee, withdrawal_fee_currency="XRP",
                               expected_received=expected_received, withdrawal_chain="XRP", raw_status=raw_status,
                               withdrawal_id=f"wd-{raw_status}-{size}", client_oid=f"abw-{size}", executed_at=utcnow())


async def test_exclusive_withdrawal_takes_size_plus_fee_off_the_exchange(db, user, bitget_key, monkeypatch):
    """Exclusive (fee on top): sent 1.1 XRP, fee 0.2 -> the balance drops by 1.3, the wallet receives 1.1.
    The mode is read from the row (expected_received == size), so flipping today's setting changes nothing."""
    purchases = await _seed(db, user, bitget_key)
    xrp = purchases["XRP"]
    await db.refresh(xrp, ["transactions"])
    buy = xrp.transactions[0]
    db.add(_withdrawal(xrp.id, buy.id, size=1.1, fee=0.2, expected_received=1.1))
    await db.commit()
    from api.config import settings
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "inclusive")   # must not matter
    pos = {p.crypto: p for p in await history.positions(db, user.id)}["XRP"]
    assert pos.acquired == pytest.approx(1.3320666, abs=1e-12)
    assert pos.on_exchange == pytest.approx(0.0320666, abs=1e-12)
    assert pos.sent_to_wallet == pytest.approx(1.1, abs=1e-12)
    assert pos.withdrawals == 1 and pos.fee_modes == ["exclusive"]


async def test_inclusive_withdrawal_takes_only_the_size_off_the_exchange(db, user, bitget_key, monkeypatch):
    """Inclusive (fee deducted): sent 1.1 XRP, fee 0.2 -> the balance drops by 1.1, the wallet receives 0.9."""
    purchases = await _seed(db, user, bitget_key)
    xrp = purchases["XRP"]
    await db.refresh(xrp, ["transactions"])
    buy = xrp.transactions[0]
    db.add(_withdrawal(xrp.id, buy.id, size=1.1, fee=0.2, expected_received=0.9))
    await db.commit()
    from api.config import settings
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "exclusive")   # must not matter
    pos = {p.crypto: p for p in await history.positions(db, user.id)}["XRP"]
    assert pos.on_exchange == pytest.approx(0.2320666, abs=1e-12)
    assert pos.sent_to_wallet == pytest.approx(0.9, abs=1e-12)
    assert pos.fee_modes == ["inclusive"]


async def test_simulated_withdrawals_move_nothing(db, user, bitget_key):
    purchases = await _seed(db, user, bitget_key)
    xrp = purchases["XRP"]
    await db.refresh(xrp, ["transactions"])
    db.add(_withdrawal(xrp.id, xrp.transactions[0].id, size=1.1, fee=0.2, expected_received=1.1, raw_status="simulated"))
    await db.commit()
    pos = {p.crypto: p for p in await history.positions(db, user.id)}["XRP"]
    assert pos.on_exchange == pytest.approx(1.3320666, abs=1e-12) and pos.sent_to_wallet == 0.0 and pos.withdrawals == 0


async def test_dashboard_and_transactions_expose_net_and_location(client, db, user, bitget_key):
    from tests.conftest_autobuy import auth
    purchases = await _seed(db, user, bitget_key)
    xrp = purchases["XRP"]
    await db.refresh(xrp, ["transactions"])
    db.add(_withdrawal(xrp.id, xrp.transactions[0].id, size=1.1, fee=0.2, expected_received=1.1))
    await db.commit()
    d = (await client.get("/api/dashboard/", headers=auth(user))).json()
    by = {h["crypto"]: h for h in d["holdings"]}
    assert by["BTC"]["total_amount"] == pytest.approx(0.000196803, abs=1e-12) and by["BTC"]["on_exchange"] == by["BTC"]["total_amount"]
    assert by["XRP"] == pytest.approx({"crypto": "XRP", "total_amount": 1.3320666, "on_exchange": 0.0320666, "sent_to_wallet": 1.1,
                                       "total_cost_usdt": 1.99996666, "price_precision": 4}, abs=1e-9)
    rows = (await client.get("/api/transactions/", headers=auth(user))).json()["items"]
    buy = next(r for r in rows if r["crypto"] == "BTC" and r["tx_type"] == "buy")
    assert buy["amount_crypto"] == 0.000197, "the row keeps the gross fill, as Bitget's order history shows it"
    assert buy["net_amount"] == pytest.approx(0.000196803, abs=1e-12)
    perf = (await client.get("/api/analytics/performance", headers=auth(user))).json()
    assert {h["crypto"]: h["total_amount"] for h in perf["holdings"]}["XLM"] == pytest.approx(14.30568, abs=1e-12)


async def test_executor_stamps_every_withdrawal_row_with_the_fee_mode(db, user, bitget_key, monkeypatch):
    """A NULL withdrawal_fee_mode on a row written after migration 025 is a bug,
    not a legacy case: the derivation fallback exists for history only."""
    from api.tasks import purchase_scheduler as ps
    from tests.fake_gateway import FakeGateway
    from api.config import settings
    from sqlalchemy import select
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "inclusive")
    p = await make_purchase(db, user, bitget_key, crypto_symbol="BTC", purchase_type="autobuy")
    await db.refresh(p, ["exchange_account", "user", "transactions"])
    await ps.execute_one(db, p, p.next_execution, FakeGateway(), dry_run=True)
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id,
                                                                PurchaseTransaction.tx_type == "withdrawal"))).scalars().all()
    assert rows and all(r.withdrawal_fee_mode == "inclusive" for r in rows), [r.withdrawal_fee_mode for r in rows]
    # and a failed send (no amount) still records the mode it would have used
    p2 = await make_purchase(db, user, bitget_key, crypto_symbol="BTC", purchase_type="autobuy")
    await db.refresh(p2, ["exchange_account", "user", "transactions"])
    gw = FakeGateway(); gw.fail_withdrawal = True
    await ps.execute_one(db, p2, p2.next_execution, gw, dry_run=True)
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p2.id,
                                                                PurchaseTransaction.tx_type == "withdrawal"))).scalars().all()
    assert rows and all(r.withdrawal_fee_mode is not None for r in rows)


async def test_stored_mode_wins_over_the_derivation_and_the_check_constraint_holds(db, user, bitget_key):
    from sqlalchemy.exc import IntegrityError
    purchases = await _seed(db, user, bitget_key)
    xrp = purchases["XRP"]
    await db.refresh(xrp, ["transactions"])
    buy = xrp.transactions[0]
    # a row whose expected_received looks inclusive but whose stored mode says exclusive: the column wins
    wd = _withdrawal(xrp.id, buy.id, size=1.1, fee=0.2, expected_received=0.9)
    wd.withdrawal_fee_mode = "exclusive"
    db.add(wd); await db.commit()
    assert history.withdrawal_fee_mode(wd) == "exclusive"
    pos = {p.crypto: p for p in await history.positions(db, user.id)}["XRP"]
    assert pos.on_exchange == pytest.approx(0.0320666, abs=1e-12) and pos.sent_to_wallet == pytest.approx(1.1, abs=1e-12)
    # legacy row (NULL column) still derives from expected_received
    wd.withdrawal_fee_mode = None
    assert history.withdrawal_fee_mode(wd) == "inclusive"
    uid, kid = user.id, bitget_key.id
    bad = _withdrawal(xrp.id, buy.id, size=0.5, fee=0.2, expected_received=0.5); bad.withdrawal_fee_mode = "on_top"
    db.add(bad)
    with pytest.raises(IntegrityError) as ei:
        await db.flush()
    assert "ck_purchase_transactions_withdrawal_fee_mode" in str(ei.value)
    await db.rollback()
    await db.refresh(user); await db.refresh(bitget_key)
