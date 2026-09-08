"""POST/GET/PUT/DELETE /api/v2/purchases and /api/wallets through the real app."""
import uuid

import pytest
from sqlalchemy import select

from api.models import Purchase, PurchaseTransaction, Wallet, utcnow
from api.utils import exchange_networks as en
from tests.conftest_autobuy import (BTC_MAIN, BTC_TEST, ETH, SOL, auth, binance_key, client, db,  # noqa: F401
                                    make_key, make_purchase, other_user, user)

P = "/api/v2/purchases"


def body(key, **over):
    b = {"crypto_symbol": "BTC", "amount_usd": 50, "wallet_address": BTC_MAIN, "blockchain_network": "mainnet",
         "frequency": "daily", "exchange_account_id": str(key.id)}
    b.update(over)
    return b


async def test_requires_auth(client):
    assert (await client.post(P, json={})).status_code == 401
    assert (await client.get(P)).status_code == 401
    assert (await client.get("/api/wallets")).status_code == 401


async def test_create_with_valid_address_and_network(client, db, user, binance_key):
    r = await client.post(P, json=body(binance_key), headers=auth(user))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "active" and out["next_execution"] is not None
    row = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(out["purchase_id"])))).scalar_one()
    assert row.user_id == user.id and row.exchange_account_id == binance_key.id
    assert row.crypto_symbol == "BTC" and row.wallet_address == BTC_MAIN and row.blockchain_network == "mainnet"
    assert row.next_execution <= utcnow(), "due on the scheduler's next run"


@pytest.mark.parametrize("over, expected_error", [
    ({"wallet_address": BTC_TEST, "blockchain_network": "mainnet"}, "invalid_wallet_address"),  # testnet addr, mainnet purchase
    ({"wallet_address": BTC_MAIN, "blockchain_network": "testnet"}, "invalid_wallet_address"),  # mainnet addr, testnet purchase
    ({"wallet_address": ETH}, "invalid_wallet_address"),                                        # ETH address for BTC
    ({"crypto_symbol": "ETH", "wallet_address": SOL}, "invalid_wallet_address"),                 # SOL address for ETH
    ({"crypto_symbol": "USDT", "wallet_address": BTC_MAIN}, "invalid_wallet_address"),           # USDT is ERC-20
    ({"wallet_address": BTC_MAIN[:-1] + "b"}, "invalid_wallet_address"),                          # bad checksum
])
async def test_create_rejects_mismatched_address_network(client, db, user, binance_key, over, expected_error):
    r = await client.post(P, json=body(binance_key, **over), headers=auth(user))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == expected_error
    assert (await db.execute(select(Purchase).where(Purchase.user_id == user.id))).first() is None


async def test_create_rejects_unsupported_exchange_network(client, db, user, binance_key, monkeypatch):
    monkeypatch.setitem(en.EXCHANGE_NETWORKS, "binance", ["mainnet"])
    r = await client.post(P, json=body(binance_key, wallet_address=BTC_TEST, blockchain_network="testnet"), headers=auth(user))
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["error"] == "network_not_supported_by_exchange"
    assert d["details"] == {"exchange": "binance", "blockchain_network": "testnet", "supported_networks": ["mainnet"]}


async def test_create_rejects_foreign_or_missing_exchange_account(client, db, user, other_user):
    foreign = await make_key(db, other_user)
    r = await client.post(P, json=body(foreign), headers=auth(user))
    assert r.status_code == 404 and r.json()["detail"]["error"] == "exchange_account_not_found"
    r = await client.post(P, json=body(foreign, exchange_account_id=str(uuid.uuid4())), headers=auth(user))
    assert r.status_code == 404


@pytest.mark.parametrize("over", [
    {"amount_usd": 5}, {"amount_usd": 2_000_000}, {"crypto_symbol": "DOGE"}, {"frequency": "hourly"},
    {"blockchain_network": "devnet"}, {"wallet_address": "short"},
])
async def test_create_schema_validation(client, user, binance_key, over):
    r = await client.post(P, json=body(binance_key, **over), headers=auth(user))
    assert r.status_code == 422


async def test_frequency_aliases_and_case(client, user, binance_key):
    r = await client.post(P, json=body(binance_key, frequency="One-Time", crypto_symbol="btc"), headers=auth(user))
    assert r.status_code == 201
    r = await client.get(f"{P}/{r.json()['purchase_id']}", headers=auth(user))
    assert r.json()["frequency"] == "one_time" and r.json()["crypto_symbol"] == "BTC"


async def test_list_returns_all_user_purchases_with_latest_transaction(client, db, user, other_user, binance_key):
    p1 = await make_purchase(db, user, binance_key)
    p2 = await make_purchase(db, user, binance_key, crypto_symbol="ETH", wallet_address=ETH)
    await make_purchase(db, other_user, await make_key(db, other_user))
    older = PurchaseTransaction(purchase_id=p1.id, status="failed", error_message="boom", executed_at=utcnow())
    db.add(older)
    await db.commit()
    newer = PurchaseTransaction(purchase_id=p1.id, status="success", exchange_tx_id="wd-1", amount_executed=0.001,
                                fee=0.0001, executed_at=utcnow())
    db.add(newer)
    await db.commit()

    r = await client.get(P, headers=auth(user))
    assert r.status_code == 200
    rows = {x["id"]: x for x in r.json()}
    assert set(rows) == {str(p1.id), str(p2.id)}, "only the caller's purchases"
    assert rows[str(p1.id)]["latest_transaction"]["exchange_tx_id"] == "wd-1"
    assert rows[str(p1.id)]["exchange"] == "binance"
    assert rows[str(p2.id)]["latest_transaction"] is None


async def test_detail_has_full_history_and_filter(client, db, user, other_user, binance_key):
    p = await make_purchase(db, user, binance_key)
    for st in ("failed", "success", "pending"):
        db.add(PurchaseTransaction(purchase_id=p.id, status=st, executed_at=utcnow()))
        await db.commit()
    r = await client.get(f"{P}/{p.id}", headers=auth(user))
    assert r.status_code == 200
    assert [t["status"] for t in r.json()["transactions"]] == ["pending", "success", "failed"], "newest first"
    r = await client.get(f"{P}/{p.id}/transactions", params={"status_filter": "failed"}, headers=auth(user))
    assert [t["status"] for t in r.json()] == ["failed"]
    assert (await client.get(f"{P}/{p.id}", headers=auth(other_user))).status_code == 404
    assert (await client.get(f"{P}/{uuid.uuid4()}", headers=auth(user))).status_code == 404


async def test_pause_resume_update_and_cancel(client, db, user, binance_key):
    p = await make_purchase(db, user, binance_key)
    H = auth(user)
    r = await client.put(f"{P}/{p.id}", json={"status": "paused"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = await client.put(f"{P}/{p.id}", json={"status": "active", "amount_usd": 75, "frequency": "weekly"}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "active" and r.json()["amount_usd"] == 75 and r.json()["frequency"] == "weekly"
    assert (await client.put(f"{P}/{p.id}", json={"status": "completed"}, headers=H)).status_code == 422
    assert (await client.put(f"{P}/{p.id}", json={}, headers=H)).status_code == 400

    r = await client.delete(f"{P}/{p.id}", headers=H)
    assert r.status_code == 204
    await db.refresh(p)
    assert p.status == "cancelled" and p.next_execution is None
    assert (await client.put(f"{P}/{p.id}", json={"status": "active"}, headers=H)).status_code == 409
    assert (await client.get(f"{P}/{p.id}", headers=H)).status_code == 200, "history is kept"


# --------------------------------------------------------------------------- wallets

async def test_wallet_crud_and_validation(client, db, user, other_user):
    H = auth(user)
    r = await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "mainnet", "label": "cold"}, headers=H)
    assert r.status_code == 201, r.text
    w = r.json()
    assert w["chain"] == "bitcoin" and w["label"] == "cold" and w["verified_at"]
    r = await client.post("/api/wallets", json={"address": ETH, "blockchain_network": "testnet"}, headers=H)
    assert r.status_code == 201 and r.json()["chain"] == "ethereum"

    # format must match the declared network
    r = await client.post("/api/wallets", json={"address": BTC_TEST, "blockchain_network": "mainnet"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["details"]["detected_network"] == "testnet"
    assert (await client.post("/api/wallets", json={"address": "0xnotanaddress0000000000", "blockchain_network": "mainnet"}, headers=H)).status_code == 422
    # duplicates
    assert (await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "mainnet"}, headers=H)).status_code == 409
    assert (await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "mainnet"}, headers=auth(other_user))).status_code == 409

    # list + network filter
    assert {x["address"] for x in (await client.get("/api/wallets", headers=H)).json()} == {BTC_MAIN, ETH}
    assert [x["address"] for x in (await client.get("/api/wallets", params={"blockchain_network": "testnet"}, headers=H)).json()] == [ETH]
    assert (await client.get("/api/wallets", headers=auth(other_user))).json() == []

    # delete (own only)
    assert (await client.delete(f"/api/wallets/{w['id']}", headers=auth(other_user))).status_code == 404
    assert (await client.delete(f"/api/wallets/{w['id']}", headers=H)).status_code == 204
    assert (await db.execute(select(Wallet).where(Wallet.id == uuid.UUID(w["id"])))).first() is None


# --------------------------------------------------------------------------- modes: autobuy vs dca

async def test_default_purchase_type_is_autobuy(client, db, user, binance_key):
    r = await client.post(P, json=body(binance_key), headers=auth(user))
    assert r.status_code == 201
    row = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(r.json()["purchase_id"])))).scalar_one()
    assert row.purchase_type == "autobuy"
    out = (await client.get(f"{P}/{row.id}", headers=auth(user))).json()
    assert out["purchase_type"] == "autobuy" and out["withdraw_immediately"] is True


async def test_create_dca_purchase(client, db, user, binance_key):
    r = await client.post(P, json=body(binance_key, crypto_symbol="ETH", wallet_address=ETH, purchase_type="dca"), headers=auth(user))
    assert r.status_code == 201, r.text
    row = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(r.json()["purchase_id"])))).scalar_one()
    assert row.purchase_type == "dca" and row.wallet_address == ETH
    listed = (await client.get(P, headers=auth(user))).json()[0]
    assert listed["purchase_type"] == "dca" and listed["withdraw_immediately"] is False


async def test_purchase_type_validation_and_alias(client, user, binance_key):
    H = auth(user)
    assert (await client.post(P, json=body(binance_key, purchase_type="hodl"), headers=H)).status_code == 422
    # deprecated boolean alias still maps onto purchase_type
    r = await client.post(P, json=body(binance_key, withdraw_immediately=False), headers=H)
    assert r.status_code == 201
    assert (await client.get(f"{P}/{r.json()['purchase_id']}", headers=H)).json()["purchase_type"] == "dca"
    r = await client.post(P, json=body(binance_key, purchase_type="AutoBuy"), headers=H)
    assert r.status_code == 201, "case-insensitive"
    # contradictory alias is rejected rather than silently picking one
    assert (await client.post(P, json=body(binance_key, purchase_type="dca", withdraw_immediately=True), headers=H)).status_code == 422


@pytest.mark.parametrize("purchase_type", ["autobuy", "dca"])
async def test_both_modes_require_wallet_and_network(client, user, binance_key, purchase_type):
    H = auth(user)
    b = body(binance_key, purchase_type=purchase_type); del b["wallet_address"]
    assert (await client.post(P, json=b, headers=H)).status_code == 422
    assert (await client.post(P, json=body(binance_key, purchase_type=purchase_type, wallet_address=""), headers=H)).status_code == 422
    b = body(binance_key, purchase_type=purchase_type); del b["blockchain_network"]
    assert (await client.post(P, json=b, headers=H)).status_code == 422
    # and the wallet must be valid for the coin/network in both modes
    r = await client.post(P, json=body(binance_key, purchase_type=purchase_type, wallet_address=BTC_TEST), headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_wallet_address"


async def test_dca_skips_exchange_withdrawal_network_check(client, user, binance_key, monkeypatch):
    monkeypatch.setitem(en.EXCHANGE_NETWORKS, "binance", ["mainnet"])
    payload = body(binance_key, wallet_address=BTC_TEST, blockchain_network="testnet")
    assert (await client.post(P, json={**payload, "purchase_type": "autobuy"}, headers=auth(user))).status_code == 422
    assert (await client.post(P, json={**payload, "purchase_type": "dca"}, headers=auth(user))).status_code == 201, \
        "nothing is withdrawn, so the exchange's withdrawal networks do not matter"


async def test_transactions_expose_tx_type(client, db, user, binance_key):
    p = await make_purchase(db, user, binance_key, purchase_type="dca")
    db.add(PurchaseTransaction(purchase_id=p.id, status="success", tx_type="accumulation", exchange_tx_id="ord-1", executed_at=utcnow()))
    await db.commit()
    out = (await client.get(f"{P}/{p.id}", headers=auth(user))).json()
    assert out["latest_transaction"]["tx_type"] == "accumulation"
    assert out["transactions"][0]["tx_type"] == "accumulation"
