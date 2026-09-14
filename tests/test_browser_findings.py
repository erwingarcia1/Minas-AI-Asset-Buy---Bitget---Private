"""Fixes from the 2026-09-13 manual browser test of the dry run:

1. withdrawal sizing honours BITGET_WITHDRAW_FEE_MODE and the live balance
2. USDT is not a purchase coin (see test_purchases_api); plain 503 messages
3. crypto amounts are shown with at most 8 decimals, trailing zeros trimmed
4. dry-run withdrawals present as "simulated" (API, transactions, emails, CSV)
5. accumulate purchases carry no chain
6. purchases whose exchange key was deleted are stopped with a clear message
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from api.config import settings
from api.models import Purchase, PurchaseTransaction
from api.services import bitget_market as bm
from api.services.history import fmt_amount
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import BTC_MAIN, auth, binance_key, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for

BTC_CHAIN = {"chain": "BTC", "withdrawable": True, "withdraw_fee": 0.00003, "extra_withdraw_fee_pct": 0.0,
             "min_withdraw_amount": 0.0005, "withdraw_min_scale": 8}
PCT_CHAIN = {**BTC_CHAIN, "chain": "PCT", "extra_withdraw_fee_pct": 0.1}   # 10% extra fee, like Bitget's field
SYMBOL = {"symbol": "BTCUSDT", "online": True, "min_trade_usdt": 1.0, "taker_fee_rate": 0.002, "quantity_precision": 6}
P = "/api/v2/purchases"


# --------------------------------------------------------------------------- 1. sizing

def test_withdrawal_plan_exclusive_sends_net_minus_fee_and_needs_balance_for_both():
    plan = bm.withdrawal_plan(0.00076179, BTC_CHAIN, "exclusive")
    assert plan == {"mode": "exclusive", "size": 0.00073179, "fee": 0.00003, "received": 0.00073179,
                    "balance_needed": pytest.approx(0.00076179), "sendable": True}


def test_withdrawal_plan_inclusive_sends_net_and_exchange_deducts_the_fee():
    plan = bm.withdrawal_plan(0.00076179, BTC_CHAIN, "inclusive")
    assert plan["size"] == 0.00076179 and plan["fee"] == 0.00003
    assert plan["received"] == pytest.approx(0.00073179) and plan["balance_needed"] == 0.00076179 and plan["sendable"]


def test_withdrawal_plan_floors_to_chain_scale_and_handles_percentage_fees():
    scaled = bm.withdrawal_plan(0.123456789, {**BTC_CHAIN, "withdraw_min_scale": 4}, "exclusive")
    assert scaled["size"] == 0.1234 and scaled["fee"] == 0.00003
    pct = bm.withdrawal_plan(0.01, PCT_CHAIN, "exclusive")
    assert pct["size"] + pct["fee"] <= 0.01 + 1e-12 and pct["fee"] == pytest.approx(0.00003 + pct["size"] * 0.1)
    dust = bm.withdrawal_plan(0.00002, BTC_CHAIN, "exclusive")
    assert dust["size"] == 0 and not dust["sendable"]


def test_fee_mode_comes_from_settings_and_falls_back_to_exclusive(monkeypatch):
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "inclusive")
    assert bm.fee_mode() == "inclusive" and bm.withdrawal_plan(0.001, BTC_CHAIN)["size"] == 0.001
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "bogus")
    assert bm.fee_mode() == "exclusive"


def test_minimum_check_uses_the_amount_actually_sent(monkeypatch):
    price = 78_960.0
    # 41 USD buys ~0.00051826 net: enough for the 0.0005 minimum only if the fee is deducted from it (inclusive)
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "exclusive")
    est = bm.estimate_received(41, price, SYMBOL, BTC_CHAIN)
    assert est["withdraw_size"] < 0.0005 and not bm.meets_minimum(est)
    with pytest.raises(bm.PurchaseValidationError) as exc:
        bm.check_withdrawal("BTC", 41, price, SYMBOL, BTC_CHAIN)
    assert exc.value.details["fee_mode"] == "exclusive" and exc.value.details["minimum_usd"] > 41
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "inclusive")
    est = bm.estimate_received(41, price, SYMBOL, BTC_CHAIN)
    assert est["withdraw_size"] >= 0.0005 and bm.meets_minimum(est)
    assert bm.check_withdrawal("BTC", 41, price, SYMBOL, BTC_CHAIN)["received"] == pytest.approx(est["withdraw_size"] - 0.00003)
    assert bm.minimum_purchase_usd(price, SYMBOL, BTC_CHAIN, "inclusive") < bm.minimum_purchase_usd(price, SYMBOL, BTC_CHAIN, "exclusive")


class Venue:
    """Stand-in for the exchange client: fixed price, configurable balance, records withdrawals."""
    available = 1.0
    withdrawals = []
    exchange = "bitget"

    def __init__(self, *a, **k): pass
    def get_available(self, coin): return Venue.available
    def withdraw(self, coin, address, amount, network=None, client_oid=None, dry_run=True, travel_rule=None):
        Venue.withdrawals.append({"amount": amount, "network": network, "dry_run": dry_run})
        return {"withdrawal_id": "DRY" if dry_run else "WD-live", "amount": amount, "dry_run": dry_run}


@pytest.fixture
def venue(monkeypatch):
    Venue.available, Venue.withdrawals = 1.0, []
    monkeypatch.setattr(ps, "_client_for", lambda account: Venue())
    return Venue


@pytest.mark.parametrize("mode, expected_size, expected_received", [
    ("exclusive", 0.00073179, 0.00073179),
    ("inclusive", 0.00076179, 0.00073179),
])
async def test_gateway_sizes_the_withdrawal_per_fee_mode_and_stores_both_amounts(db, user, bitget_key, venue, monkeypatch, mode,
                                                                                 expected_size, expected_received):
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", mode)
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, next_execution=NOW)
    result = ps.ExchangeClientGateway().withdraw(bitget_key, p, 0.00076179, "abw-1", True)
    assert result["size"] == expected_size and result["expected_received"] == pytest.approx(expected_received)
    assert result["withdrawal_fee"] == 0.00003 and result["raw_status"] == "simulated"
    assert venue.withdrawals == [{"amount": expected_size, "network": "BTC", "dry_run": True}]


async def test_live_withdrawal_checks_the_available_balance_first(db, user, bitget_key, venue, monkeypatch):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, next_execution=NOW)
    gw = ps.ExchangeClientGateway()
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "exclusive")
    venue.available = 0.00076   # 0.00073179 size + 0.00003 fee = 0.00076179 needed
    with pytest.raises(ps.ExecutionError) as exc:
        gw.withdraw(bitget_key, p, 0.00076179, "abw-2", False)
    assert exc.value.code == "INSUFFICIENT_BALANCE" and venue.withdrawals == [], "nothing sent when the fee would not be covered"
    venue.available = 0.00076179
    assert gw.withdraw(bitget_key, p, 0.00076179, "abw-3", False)["withdrawal_id"] == "WD-live"
    # inclusive: the size itself is all that must be on the account
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "inclusive")
    venue.available = 0.00076179
    assert gw.withdraw(bitget_key, p, 0.00076179, "abw-4", False)["size"] == 0.00076179
    # dry runs never read the balance
    venue.available = 0.0
    assert gw.withdraw(bitget_key, p, 0.00076179, "abw-5", True)["raw_status"] == "simulated"


async def test_gateway_refuses_sizes_below_the_minimum_after_the_fee(db, user, bitget_key, venue, monkeypatch):
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "exclusive")
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=41, next_execution=NOW)
    with pytest.raises(ps.ExecutionError) as exc:
        ps.ExchangeClientGateway().withdraw(bitget_key, p, 0.00052, "abw-6", True)   # 0.00049 after the fee < 0.0005
    assert exc.value.code == "WITHDRAW_BELOW_MINIMUM" and venue.withdrawals == []


async def test_executor_records_amount_sent_and_expected_received(db, user, bitget_key, venue, monkeypatch):
    monkeypatch.setattr(settings, "bitget_withdraw_fee_mode", "exclusive")
    monkeypatch.setattr(bm, "live_ask", lambda coin: 78_960.0)

    class Buy(FakeGateway):
        PRICE = 78_960.0
        FEE_RATE = 0.002
        def withdraw(self, account, purchase, amount, client_oid, dry_run):
            return ps.ExchangeClientGateway().withdraw(account, purchase, amount, client_oid, dry_run)
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=Buy(), dry_run=True)
    buy, wd = await tx_for(db, p)
    net = buy.amount_executed - buy.fee
    assert wd.amount_executed == pytest.approx(net - 0.00003, abs=1e-8) and wd.expected_received == wd.amount_executed
    assert wd.withdrawal_fee == 0.00003 and wd.withdrawal_fee_currency == "BTC"


# --------------------------------------------------------------------------- 3. formatting

def test_fmt_amount_trims_to_eight_decimals_without_trailing_zeros():
    assert fmt_amount(0.0007617900) == "0.00076179"
    assert fmt_amount(0.00000153) == "0.00000153"
    assert fmt_amount(1.5e-06) == "0.0000015"
    assert fmt_amount(0.5) == "0.5" and fmt_amount(2.0) == "2" and fmt_amount(0) == "0"
    assert fmt_amount(0.123456789123) == "0.12345679"
    assert fmt_amount(None) == ""


# --------------------------------------------------------------------------- 4. simulated status

@pytest.fixture
def sent_emails(monkeypatch):
    from api.services.email_service import email_service
    seen = []

    async def fake(to, subject, html, text, template=None, **kw):
        seen.append({"to": to, "subject": subject, "text": text})
        return True
    monkeypatch.setattr(email_service, "_send", fake)
    monkeypatch.setattr(email_service, "queue", fake)   # purchase emails are queued, not sent inline
    return seen


async def test_dry_run_withdrawal_is_presented_as_simulated_everywhere(db, user, bitget_key, client, sent_emails):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, frequency="one_time", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    buy, wd = await tx_for(db, p)
    assert wd.status == "success" and wd.raw_status == "simulated", "storage keeps the executor outcome"
    c = client
    detail = (await c.get(f"{P}/{p.id}", headers=auth(user))).json()
    statuses = {t["tx_type"]: t["status"] for t in detail["transactions"]}
    assert statuses == {"buy": "success", "withdrawal": "simulated"}
    assert detail["latest_transaction"]["status"] == "simulated"
    rows = (await c.get(f"{P}/{p.id}/transactions", headers=auth(user))).json()
    assert {t["tx_type"]: t["status"] for t in rows} == {"buy": "success", "withdrawal": "simulated"}
    listed = (await c.get("/api/transactions/", headers=auth(user))).json()["items"]
    assert next(t for t in listed if t["tx_type"] == "withdrawal")["status"] == "simulated"
    assert next(t for t in listed if t["tx_type"] == "buy")["status"] == "completed"
    only = (await c.get("/api/transactions/", params={"status_filter": "simulated"}, headers=auth(user))).json()
    assert only["total"] == 1 and only["items"][0]["tx_type"] == "withdrawal"
    completed = (await c.get("/api/transactions/", params={"status_filter": "completed"}, headers=auth(user))).json()
    assert [t["tx_type"] for t in completed["items"]] == ["buy"], "simulated rows are not 'completed'"
    csv = (await c.get("/api/reports/tax-summary", params={"year": NOW.year, "format": "csv"}, headers=auth(user))).text
    header, *lines = csv.strip().splitlines()
    cols = header.split(",")
    lot = next(l for l in lines if "ORD-1" in l).split(",")
    assert lot[cols.index("withdrawal_status")] == "simulated" and lot[cols.index("tx_type")] == "withdrawal"
    assert lot[cols.index("quantity")] == fmt_amount(buy.amount_executed) and "." in lot[cols.index("quantity")]
    assert not lot[cols.index("quantity")].endswith("0")
    mail = [m for m in sent_emails if "Purchase complete" in m["subject"]]
    assert mail and "simulated" in mail[-1]["subject"] and "status: simulated" in mail[-1]["text"] and "nothing was sent" in mail[-1]["text"]
    assert f"{fmt_amount(wd.amount_executed)} BTC" in mail[-1]["subject"]


async def test_real_withdrawal_is_still_completed(db, user, bitget_key, client):
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", amount_usd=60, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=False)
    rows = (await client.get(f"{P}/{p.id}/transactions", headers=auth(user))).json()
    assert {t["tx_type"]: t["status"] for t in rows} == {"buy": "success", "withdrawal": "success"}
    listed = (await client.get("/api/transactions/", params={"status_filter": "completed"}, headers=auth(user))).json()
    assert sorted(t["tx_type"] for t in listed["items"]) == ["buy", "withdrawal"]


# --------------------------------------------------------------------------- 5. accumulate: no chain

async def test_accumulate_purchase_stores_no_chain_unless_a_wallet_is_kept(db, user, bitget_key, client):
    c = client
    r = await c.post(P, json={"crypto_symbol": "BTC", "amount_usd": 60, "frequency": "weekly",
                              "exchange_account_id": str(bitget_key.id), "purchase_type": "dca"}, headers=auth(user))
    assert r.status_code == 201, r.text
    held = (await c.get(f"{P}/{r.json()['purchase_id']}", headers=auth(user))).json()
    assert held["blockchain_network"] is None and held["wallet_address"] is None and held["purchase_type"] == "dca"
    row = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(held["id"])))).scalar_one()
    assert row.blockchain_network is None
    # a wallet kept on file for later still needs its chain (defaulted from the coin)
    r = await c.post(P, json={"crypto_symbol": "BTC", "amount_usd": 60, "frequency": "weekly", "wallet_address": BTC_MAIN,
                              "exchange_account_id": str(bitget_key.id), "purchase_type": "dca"}, headers=auth(user))
    assert r.status_code == 201 and (await c.get(f"{P}/{r.json()['purchase_id']}", headers=auth(user))).json()["blockchain_network"] == "BTC"
    # accumulate executes without a chain, and switching to withdraw picks the chain up from the switch
    p = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(held["id"])))).scalar_one()
    p.next_execution = NOW
    await db.commit()
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    (buy,) = await tx_for(db, p)
    assert buy.status == "success" and buy.tx_type == "buy"
    sw = await c.put(f"{P}/{p.id}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN, "confirm_wallet_address": BTC_MAIN},
                     headers=auth(user))
    assert sw.status_code == 200, sw.text
    assert sw.json()["blockchain_network"] == "BTC" and sw.json()["purchase_type"] == "autobuy"


# --------------------------------------------------------------------------- 6. exchange key removed

async def test_deleting_a_key_stops_its_purchases_with_a_clear_message(db, user, client):
    key = await make_key(db, user, exchange="bitget")
    active = await make_purchase(db, user, key, purchase_type="dca", blockchain_network=None, wallet_address=None, next_execution=NOW)
    done = await make_purchase(db, user, key, purchase_type="dca", status="completed", next_execution=None)
    c = client
    assert (await c.delete(f"/api/exchange/keys/{key.id}", headers=auth(user))).status_code == 204
    await db.refresh(active)
    await db.refresh(done)
    assert active.status == "paused" and active.next_execution is None and active.exchange_account_id is None
    assert active.last_error == ps.KEY_REMOVED_MESSAGE
    assert done.status == "completed", "finished purchases are left alone"
    listed = next(p for p in (await c.get(P, headers=auth(user))).json() if p["id"] == str(active.id))
    assert listed["exchange_key_removed"] is True and listed["exchange"] is None and listed["last_error"].startswith("Exchange key removed")
    r = await c.put(f"{P}/{active.id}", json={"status": "active"}, headers=auth(user))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "exchange_key_removed"
    assert "cannot run again" in r.json()["detail"]["details"]["message"]
    # pausing, editing the amount and cancelling still work
    assert (await c.put(f"{P}/{active.id}", json={"amount_usd": 80}, headers=auth(user))).status_code == 200
    assert (await c.delete(f"{P}/{active.id}", headers=auth(user))).status_code == 204


async def test_executor_stops_a_purchase_whose_key_vanished(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    p.exchange_account_id = None
    await db.commit()
    gw = FakeGateway()
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    await db.refresh(p)
    assert counts["failed"] == 1 and gw.placed == []
    assert p.status == "failed" and p.next_execution is None and p.last_error == ps.KEY_REMOVED_MESSAGE
    (tx,) = await tx_for(db, p)
    assert tx.status == "failed" and tx.error_message == ps.KEY_REMOVED_MESSAGE
