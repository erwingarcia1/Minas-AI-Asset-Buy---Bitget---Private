"""Destination tag / memo path for XRP Ledger and Stellar (migration 023).

The rule, enforced at purchase creation, on the switch to withdraw-immediately,
on /api/wallets and again by the executor before any send:
  * a tag chain needs the destination type answered (self_custody | exchange);
  * an exchange destination needs a valid memo; self-custody must not carry one;
  * an X-address / muxed M-address supplies the tag itself; a conflicting typed
    memo is refused; the stored address is the base (classic) address.
Chains without a tag refuse a memo or destination type outright.
"""
import pytest
from sqlalchemy import delete, select

from api.models import Purchase, PurchaseTransaction, Wallet
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import auth, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway

P = "/api/v2/purchases"
# Generated with the same Base58Check / StrKey maths the validators use; checksums verified (no funds).
XRP = "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePL"
XRP_X = "XVa6FLVfDCaBZAaHruwN9KDV4Rh8paJ9S75izdBzZxZnSHJ"          # X-address: classic below, tag 12345
XRP_X_CLASSIC = "rUQL2YfQWq4qDyfbcGMiiscW5ebJNPQHyX"
XLM = "GAWUTTOC7KHJAAM6ZTUNS6DP7MQFWKDGKTF2NCXAPQ2ZREFGDQJ2UK34"
XLM_M = "MBIFHKTHAKIL3RCVFJ6MWKYONQRSR5PRYVT7HHI23UHMTQXMZ3GHCAAAAAAAAAADBFFOU"   # muxed: memo id 777
XLM_M_CLASSIC = "GBIFHKTHAKIL3RCVFJ6MWKYONQRSR5PRYVT7HHI23UHMTQXMZ3GHDSFC"
BTC = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"


def body(key, **over):
    b = {"crypto_symbol": "XRP", "amount_usd": 50, "wallet_address": XRP, "blockchain_network": "XRP",
         "frequency": "daily", "exchange_account_id": str(key.id)}
    b.update(over)
    return b


def err(r):
    return r.json()["detail"]["error"]


@pytest.mark.parametrize("over, expected", [
    ({}, "destination_type_required"),                                   # never said what the address is
    ({"destination_type": "exchange"}, "memo_required"),                  # exchange without its tag
    ({"destination_type": "exchange", "memo": ""}, "memo_required"),
    ({"destination_type": "exchange", "memo": "abc"}, "invalid_memo"),    # XRP tags are digits
    ({"destination_type": "exchange", "memo": "4294967296"}, "invalid_memo"),
    ({"destination_type": "self_custody", "memo": "12345"}, "memo_not_expected"),
    ({"destination_type": "vault"}, "destination_type_required"),
    ({"wallet_address": XRP_X, "destination_type": "exchange", "memo": "999"}, "memo_conflict"),   # X-address says 12345
    ({"wallet_address": XRP_X, "destination_type": "self_custody"}, "memo_conflict"),
    ({"wallet_address": "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePX"}, "invalid_wallet_address"),          # checksum
])
async def test_xrp_purchase_refusals_are_server_side(client, user, bitget_key, over, expected):
    r = await client.post(P, json=body(bitget_key, **over), headers=auth(user))
    assert r.status_code == 422, r.text
    assert err(r) == expected, r.text
    assert r.json()["detail"]["details"]["needs_tag"] is True or expected == "invalid_wallet_address"


async def test_xrp_exchange_with_tag_and_self_custody_are_accepted(client, db, user, bitget_key):
    H = auth(user)
    r = await client.post(P, json=body(bitget_key, destination_type="exchange", memo=" 12345 "), headers=H)
    assert r.status_code == 201, r.text
    p1 = (await client.get(f"{P}/{r.json()['purchase_id']}", headers=H)).json()
    assert p1["wallet_destination_type"] == "exchange" and p1["wallet_memo"] == "12345" and p1["blockchain_network"] == "XRP"
    r = await client.post(P, json=body(bitget_key, destination_type="self_custody"), headers=H)
    assert r.status_code == 201, r.text
    p2 = (await client.get(f"{P}/{r.json()['purchase_id']}", headers=H)).json()
    assert p2["wallet_destination_type"] == "self_custody" and p2["wallet_memo"] is None


async def test_x_address_supplies_the_tag_and_is_stored_as_classic(client, user, bitget_key):
    H = auth(user)
    r = await client.post(P, json=body(bitget_key, wallet_address=XRP_X, destination_type="exchange"), headers=H)
    assert r.status_code == 201, r.text
    p = (await client.get(f"{P}/{r.json()['purchase_id']}", headers=H)).json()
    assert p["wallet_address"] == XRP_X_CLASSIC and p["wallet_memo"] == "12345" and p["wallet_destination_type"] == "exchange"
    # the same tag typed explicitly is fine
    r = await client.post(P, json=body(bitget_key, wallet_address=XRP_X, destination_type="exchange", memo="12345"), headers=H)
    assert r.status_code == 201, r.text


async def test_stellar_memo_rules(client, user, bitget_key):
    H = auth(user)
    b = lambda **o: body(bitget_key, **{"crypto_symbol": "XLM", "blockchain_network": "StellarLumens", "wallet_address": XLM, "amount_usd": 60, **o})
    assert err(await client.post(P, json=b(), headers=H)) == "destination_type_required"
    assert err(await client.post(P, json=b(destination_type="exchange"), headers=H)) == "memo_required"
    assert err(await client.post(P, json=b(destination_type="exchange", memo="x" * 29), headers=H)) == "invalid_memo"
    r = await client.post(P, json=b(destination_type="exchange", memo="deposit-42"), headers=H)   # text memo ≤ 28 bytes
    assert r.status_code == 201, r.text
    r = await client.post(P, json=b(wallet_address=XLM_M, destination_type="exchange"), headers=H)
    assert r.status_code == 201, r.text
    p = (await client.get(f"{P}/{r.json()['purchase_id']}", headers=H)).json()
    assert p["wallet_address"] == XLM_M_CLASSIC and p["wallet_memo"] == "777"


async def test_non_tag_chains_refuse_memo_and_destination_type(client, user, bitget_key):
    H = auth(user)
    r = await client.post(P, json=body(bitget_key, crypto_symbol="BTC", blockchain_network="BTC", wallet_address=BTC, memo="123"), headers=H)
    assert r.status_code == 422 and err(r) == "memo_not_used"
    r = await client.post(P, json=body(bitget_key, crypto_symbol="BTC", blockchain_network="BTC", wallet_address=BTC, destination_type="exchange"), headers=H)
    assert r.status_code == 422 and err(r) == "destination_type_not_used"
    # and an accumulate purchase with no address cannot carry them either
    r = await client.post(P, json=body(bitget_key, wallet_address="", purchase_type="dca", destination_type="exchange", memo="1"), headers=H)
    assert r.status_code == 422 and err(r) == "wallet_address_required"


async def test_switch_to_withdraw_and_wallet_change_apply_the_same_rule(client, db, user, bitget_key):
    H = auth(user)
    pid = (await client.post(P, json=body(bitget_key, wallet_address="", purchase_type="dca"), headers=H)).json()["purchase_id"]
    sw = {"purchase_type": "autobuy", "wallet_address": XRP, "confirm_wallet_address": XRP, "blockchain_network": "XRP"}
    r = await client.put(f"{P}/{pid}", json=sw, headers=H)
    assert r.status_code == 422 and err(r) == "destination_type_required"
    r = await client.put(f"{P}/{pid}", json={**sw, "destination_type": "exchange"}, headers=H)
    assert r.status_code == 422 and err(r) == "memo_required"
    r = await client.put(f"{P}/{pid}", json={**sw, "destination_type": "exchange", "memo": "77"}, headers=H)
    assert r.status_code == 200, r.text
    p = (await client.get(f"{P}/{pid}", headers=H)).json()
    assert p["purchase_type"] == "autobuy" and p["wallet_memo"] == "77" and p["wallet_destination_type"] == "exchange"
    # memo-only edit re-validates against the stored address
    r = await client.put(f"{P}/{pid}", json={"destination_type": "self_custody", "memo": ""}, headers=H)
    assert r.status_code == 200, r.text
    p = (await client.get(f"{P}/{pid}", headers=H)).json()
    assert p["wallet_destination_type"] == "self_custody" and p["wallet_memo"] is None
    r = await client.put(f"{P}/{pid}", json={"memo": "5"}, headers=H)      # self-custody + memo is a contradiction
    assert r.status_code == 422 and err(r) == "memo_not_expected"
    # changing the address to an X-address carries its tag
    r = await client.put(f"{P}/{pid}", json={"wallet_address": XRP_X, "confirm_wallet_address": XRP_X, "destination_type": "exchange"}, headers=H)
    assert r.status_code == 200, r.text
    p = (await client.get(f"{P}/{pid}", headers=H)).json()
    assert p["wallet_address"] == XRP_X_CLASSIC and p["wallet_memo"] == "12345"


async def test_wallet_address_book_stores_destination_and_memo(client, db, user):
    H = auth(user)
    w = lambda **o: {"address": XRP, "blockchain_network": "XRP", **o}
    assert err(await client.post("/api/wallets", json=w(), headers=H)) == "destination_type_required"
    assert err(await client.post("/api/wallets", json=w(destination_type="exchange"), headers=H)) == "memo_required"
    assert err(await client.post("/api/wallets", json=w(destination_type="self_custody", memo="1"), headers=H)) == "memo_not_expected"
    assert err(await client.post("/api/wallets", json={"address": BTC, "blockchain_network": "BTC", "memo": "1"}, headers=H)) == "memo_not_used"
    r = await client.post("/api/wallets", json=w(destination_type="exchange", memo="4242"), headers=H)
    assert r.status_code == 201, r.text
    assert r.json()["memo"] == "4242" and r.json()["destination_type"] == "exchange" and r.json()["needs_tag"] is True
    r = await client.post("/api/wallets", json={"address": XLM_M, "blockchain_network": "StellarLumens", "destination_type": "exchange"}, headers=H)
    assert r.status_code == 201, r.text
    assert r.json()["address"] == XLM_M_CLASSIC and r.json()["memo"] == "777"
    listed = (await client.get("/api/wallets", headers=H)).json()
    assert {x["address"]: x["memo"] for x in listed} == {XRP: "4242", XLM_M_CLASSIC: "777"}
    saved = (await db.execute(select(Wallet).where(Wallet.address == XRP))).scalar_one()
    assert saved.destination_type == "exchange" and saved.memo == "4242"


async def test_destination_check_endpoint(client, user, monkeypatch):
    from api.services import ledger_probe
    monkeypatch.setattr(ledger_probe, "destination_funded", lambda fam, addr: {"funded": False, "detail": "never seen"})
    H = auth(user)
    r = await client.get("/api/v2/market/destination", params={"chain": "XRP", "address": XRP}, headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False and j["error"] == "destination_type_required" and j["needs_tag"] is True and j["tag_label"] == "destination tag"
    assert j["funded"] is False and "has never received XRP" in j["warning"]
    r = await client.get("/api/v2/market/destination", params={"chain": "XRP", "address": XRP_X, "destination_type": "exchange"}, headers=H)
    assert r.json()["ok"] is True and r.json()["embedded_tag"] == "12345"
    r = await client.get("/api/v2/market/destination", params={"chain": "BTC", "address": BTC}, headers=H)
    assert r.json()["ok"] is True and r.json()["needs_tag"] is False and r.json()["funded"] is None and r.json()["warning"] is None


async def test_market_endpoint_labels_tag_chains(client, user):
    r = await client.get("/api/v2/market/coins/XRP", headers=auth(user))
    assert r.status_code == 200, r.text
    c = r.json()["chains"][0]
    assert c["chain"] == "XRP" and c["need_tag"] is True and c["tag_label"] == "destination tag" and c["family"] == "xrpl" and c["supported"]
    r = await client.get("/api/v2/market/coins/XLM", headers=auth(user))
    assert r.json()["chains"][0]["tag_label"] == "memo"
    r = await client.get("/api/v2/market/coins/BTC", headers=auth(user))
    assert r.json()["chains"][0]["tag_label"] is None


# ---- executor: the memo travels to the withdrawal row and the venue; sends are refused without it

async def test_executor_copies_the_memo_onto_the_withdrawal_row(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", wallet_address=XRP, blockchain_network="XRP",
                            wallet_destination_type="exchange", wallet_memo="12345", purchase_type="autobuy")
    await db.refresh(p, ["exchange_account", "user", "transactions"])
    gw = FakeGateway()
    await ps.execute_one(db, p, p.next_execution, gw, dry_run=True)
    wd = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id,
                                                              PurchaseTransaction.tx_type == "withdrawal"))).scalar_one()
    assert wd.status == "success" and wd.withdrawal_memo == "12345"


class _Venue:
    exchange = "bitget"
    calls: list = []

    def __init__(self, *a, **k): pass
    def get_available(self, coin): return 100.0
    def withdraw(self, coin, address, amount, network=None, client_oid=None, dry_run=True, travel_rule=None, tag=None):
        _Venue.calls.append({"coin": coin, "address": address, "amount": amount, "network": network, "tag": tag, "dry_run": dry_run})
        return {"withdrawal_id": "DRY-WD-1", "amount": amount, "dry_run": dry_run}


@pytest.mark.parametrize("dtype, memo, refused", [
    (None, None, "never recorded whether the address"),      # legacy row: destination never answered
    ("exchange", None, "no destination tag is stored"),
    ("exchange", "", "no destination tag is stored"),
    ("self_custody", None, None),                            # allowed, no tag sent
    ("exchange", "12345", None),                             # allowed, tag sent
])
async def test_gateway_refuses_tag_chain_sends_without_the_memo(db, user, bitget_key, monkeypatch, dtype, memo, refused):
    _Venue.calls = []
    factory_calls = []
    monkeypatch.setattr(ps, "_client_for", lambda account: factory_calls.append(1) or _Venue())
    # Built in memory, never persisted: since migration 024 the database itself refuses
    # exchange + NULL memo, so this exercises the executor's backstop on its own.
    p = Purchase(user_id=user.id, exchange_account_id=bitget_key.id, crypto_symbol="XRP", amount_usd=15.0, frequency="daily",
                 wallet_address=XRP, blockchain_network="XRP", wallet_destination_type=dtype, wallet_memo=memo,
                 purchase_type="autobuy", status="active", failure_count=0)
    gw = ps.ExchangeClientGateway()
    if refused:
        with pytest.raises(ps.ExecutionError) as ei:
            gw.withdraw(bitget_key, p, 40.0, "abw-test", True)
        assert ei.value.code == "TAG_REQUIRED" and refused in str(ei.value)
        assert _Venue.calls == [], "nothing may reach the venue"
        assert factory_calls, "the patched _client_for (via the shim) was what the gateway called"
    else:
        out = gw.withdraw(bitget_key, p, 40.0, "abw-test", True)
        assert out["memo"] == (memo or None)
        assert len(_Venue.calls) == 1 and _Venue.calls[0]["tag"] == (memo or None) and _Venue.calls[0]["network"] == "XRP"
        assert factory_calls, "the patched _client_for (via the shim) was what the gateway called"


async def test_withdrawal_parked_with_reason_when_tag_missing(db, user, bitget_key, monkeypatch):
    """End to end through the executor: buy fills, the send is refused, the
    purchase parks as withdrawal_failed with the reason; no re-buy. The row
    carries a blank memo: the one shape migration 024 cannot catch (the CHECK
    only knows NULL), so the executor must."""
    _Venue.calls = []
    factory_calls = []
    monkeypatch.setattr(ps, "_client_for", lambda account: factory_calls.append(1) or _Venue())

    class Gw(ps.ExchangeClientGateway):
        def place_buy(self, account, purchase, client_oid, dry_run):
            return {"order_id": f"DRY-{client_oid}"}
        def fetch_fill(self, account, purchase, order_id, client_oid, dry_run):
            return {"order_id": order_id, "filled": True, "raw_status": "filled", "amount_crypto": 20.0, "exchange_rate": 2.5,
                    "cost_usdt": 50.0, "fee": 0.04, "fee_currency": "XRP"}
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", wallet_address=XRP, blockchain_network="XRP",
                            wallet_destination_type="exchange", wallet_memo="   ", purchase_type="autobuy")
    await db.refresh(p, ["exchange_account", "user", "transactions"])
    await ps.execute_one(db, p, p.next_execution, Gw(), dry_run=True)
    await db.refresh(p)
    assert p.status == "withdrawal_failed" and "no destination tag is stored" in (p.last_error or "")
    assert _Venue.calls == []
    assert factory_calls, "the patched _client_for (via the shim) was what the gateway called"
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalars().all()
    assert {r.tx_type: r.status for r in rows} == {"buy": "success", "withdrawal": "failed"}


# ---- migration 024: the database itself refuses an exchange destination without a memo

@pytest.mark.parametrize("dtype, memo, writable", [
    ("exchange", None, False), ("exchange", "", True),   # empty string is the API's job; the DB only knows NULL
    ("exchange", "1", True), ("self_custody", None, True), (None, None, True), (None, "1", True),
])
async def test_db_constraint_exchange_requires_memo(db, user, bitget_key, dtype, memo, writable):
    from sqlalchemy.exc import IntegrityError
    uid, kid = user.id, bitget_key.id   # a rollback expires the fixtures' attributes
    for factory in (
        lambda: Purchase(user_id=uid, exchange_account_id=kid, crypto_symbol="XRP", amount_usd=15.0, frequency="daily",
                         wallet_address=XRP, blockchain_network="XRP", wallet_destination_type=dtype, wallet_memo=memo,
                         purchase_type="autobuy", status="active", failure_count=0),
        lambda: Wallet(user_id=uid, address=XRP, blockchain_network="XRP", destination_type=dtype, memo=memo),
    ):
        row = factory()
        db.add(row)
        if writable:
            await db.flush()
            rid, model = row.id, type(row)
            await db.commit()
            await db.execute(delete(model).where(model.id == rid))
            await db.commit()
        else:
            with pytest.raises(IntegrityError) as ei:
                await db.flush()
            assert "exchange_requires_memo" in str(ei.value)
            await db.rollback()
            await db.refresh(user)          # the rollback expired the fixtures; the teardown reads them
            await db.refresh(bitget_key)
