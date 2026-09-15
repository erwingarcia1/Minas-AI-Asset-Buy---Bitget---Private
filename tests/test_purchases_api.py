"""POST/GET/PUT/DELETE /api/v2/purchases and /api/wallets through the real app."""
import uuid

import pytest
from sqlalchemy import select

from api.models import Purchase, PurchaseTransaction, Wallet, utcnow
from tests.conftest_autobuy import (BTC_MAIN, BTC_TEST, ETH, SOL, auth, binance_key, bitget_key, client, db,  # noqa: F401
                                    make_key, make_purchase, other_user, user)

P = "/api/v2/purchases"


def body(key, **over):
    b = {"crypto_symbol": "BTC", "amount_usd": 50, "wallet_address": BTC_MAIN, "blockchain_network": "BTC",
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
    assert row.crypto_symbol == "BTC" and row.wallet_address == BTC_MAIN and row.blockchain_network == "BTC"
    assert row.next_execution <= utcnow(), "due on the scheduler's next run"


@pytest.mark.parametrize("over, expected_error", [
    ({"wallet_address": BTC_TEST, "blockchain_network": "BTC"}, "invalid_wallet_address"),    # testnet addr on the BTC chain
    ({"wallet_address": ETH}, "invalid_wallet_address"),                                      # ETH address for BTC
    ({"crypto_symbol": "ETH", "blockchain_network": "ERC20", "wallet_address": SOL}, "invalid_wallet_address"),  # SOL address on ERC20
    ({"crypto_symbol": "USDC", "blockchain_network": "ERC20", "wallet_address": BTC_MAIN}, "invalid_wallet_address"),  # USDC ERC20 needs 0x
    ({"wallet_address": BTC_MAIN[:-1] + "b"}, "invalid_wallet_address"),                        # bad checksum
])
async def test_create_rejects_mismatched_address_chain(client, db, user, binance_key, over, expected_error):
    r = await client.post(P, json=body(binance_key, **over), headers=auth(user))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == expected_error
    assert (await db.execute(select(Purchase).where(Purchase.user_id == user.id))).first() is None


async def test_chain_defaults_to_the_coins_native_chain(client, db, user, binance_key):
    b = body(binance_key); del b["blockchain_network"]
    r = await client.post(P, json=b, headers=auth(user))
    assert r.status_code == 201, r.text
    row = (await db.execute(select(Purchase).where(Purchase.id == uuid.UUID(r.json()["purchase_id"])))).scalar_one()
    assert row.blockchain_network == "BTC"
    b = body(binance_key, crypto_symbol="ETH", wallet_address=ETH); del b["blockchain_network"]
    r = await client.post(P, json=b, headers=auth(user))
    assert r.status_code == 201 and (await client.get(f"{P}/{r.json()['purchase_id']}", headers=auth(user))).json()["blockchain_network"] == "ERC20"


# --------------------------------------------------------------------------- live Bitget validation (stubbed data)

async def test_bitget_rejects_pairs_that_do_not_exist(client, user, bitget_key):
    # the stub lists no USDCUSDT pair: refused with a structured error
    r = await client.post(P, json=body(bitget_key, crypto_symbol="USDC", blockchain_network="ERC20", wallet_address=ETH, purchase_type="dca"),
                          headers=auth(user))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "pair_not_available", r.text
    assert "USDC/USDT" in r.json()["detail"]["details"]["message"]


async def test_usdt_is_not_a_purchase_coin(client, user, bitget_key):
    """Purchases are paid in USDT and Bitget has no USDTUSDT pair: refused before any exchange call."""
    r = await client.post(P, json=body(bitget_key, crypto_symbol="USDT", blockchain_network="ERC20", wallet_address=ETH, purchase_type="dca"),
                          headers=auth(user))
    assert r.status_code == 422 and "unsupported crypto" in r.text and "USDT" not in str(
        [e["msg"] for e in r.json()["detail"]]).split("choose one of")[1]


async def test_bitget_rejects_below_minimum_withdrawal_at_creation(client, user, bitget_key):
    r = await client.post(P, json=body(bitget_key, amount_usd=15), headers=auth(user))
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["error"] == "below_minimum_withdrawal" and d["details"]["chain"] == "BTC"
    assert d["details"]["min_withdraw_amount"] == 0.0005 and d["details"]["withdraw_fee"] == 0.00003
    assert 40 < d["details"]["minimum_usd"] < 45, d["details"]
    assert "Use at least about" in d["details"]["message"]
    # the same amount as accumulate is fine (nothing is withdrawn), and a large enough autobuy passes
    assert (await client.post(P, json=body(bitget_key, amount_usd=15, purchase_type="dca"), headers=auth(user))).status_code == 201
    assert (await client.post(P, json=body(bitget_key, amount_usd=50), headers=auth(user))).status_code == 201


async def test_bitget_rejects_chains_it_does_not_list_or_cannot_withdraw_on(client, user, bitget_key, monkeypatch):
    r = await client.post(P, json=body(bitget_key, blockchain_network="TRC20", wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"), headers=auth(user))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "chain_not_available"
    assert "BTC" in r.json()["detail"]["details"]["available_chains"]
    from api.services import bitget_market as bm
    monkeypatch.setattr(bm, "fetch_chains", lambda coin: [{"chain": "BTC", "withdrawable": "false", "withdrawFee": "0.00003",
                                                            "minWithdrawAmount": "0.0005", "withdrawMinScale": "8"}])
    r = await client.post(P, json=body(bitget_key, amount_usd=100), headers=auth(user))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "chain_not_withdrawable"


async def test_bitget_data_unavailable_is_503_not_a_silent_accept(client, user, bitget_key, monkeypatch):
    from api.services import bitget_market as bm
    def boom(symbol):
        raise RuntimeError("bitget down")
    monkeypatch.setattr(bm, "fetch_symbol", boom)
    r = await client.post(P, json=body(bitget_key), headers=auth(user))
    assert r.status_code == 503 and r.json()["detail"]["error"] == "exchange_data_unavailable"
    msg = r.json()["detail"]["details"]["message"]
    assert msg == bm.UNAVAILABLE_MESSAGE and "bitget:market" not in msg and "bitget down" not in msg, \
        "users get a plain message; cache keys and raw errors go to the log"
    m = await client.get("/api/v2/market/coins/BTC", headers=auth(user))
    assert m.status_code == 503 and m.json()["detail"]["details"]["message"] == bm.UNAVAILABLE_MESSAGE


async def test_market_endpoint_reports_chains_fees_and_minimum_purchase(client, user):
    r = await client.get("/api/v2/market/coins/BTC", params={"amount_usd": 15}, headers=auth(user))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["pair"] == "BTCUSDT" and d["online"] and d["price_ask"] == 77042.84 and d["taker_fee_rate"] == 0.002
    by = {c["chain"]: c for c in d["chains"]}
    assert by["BTC"]["is_default"] and by["BTC"]["family"] == "bitcoin" and by["BTC"]["withdraw_fee"] == 0.00003
    assert by["BTC"]["meets_minimum"] is False and 40 < by["BTC"]["min_purchase_usd"] < 45
    assert by["BEP20"]["family"] == "ethereum" and by["BEP20"]["meets_minimum"] is True
    assert by["LIGHTNING"]["supported"] is False and by["LIGHTNING"]["withdrawable"] is False
    assert (await client.get("/api/v2/market/coins/DOGE", headers=auth(user))).status_code == 404
    assert (await client.get("/api/v2/market/coins/USDT", headers=auth(user))).status_code == 404


async def test_exchange_flag_limits_key_creation(client, user, monkeypatch):
    assert (await client.get("/api/exchange/supported")).json() == ["bitget"]
    r = await client.post("/api/exchange/keys", json={"exchange": "binance", "api_key": "x" * 20, "api_secret": "y" * 20}, headers=auth(user))
    assert r.status_code == 422 and "bitget" in r.text
    from api.config import settings
    monkeypatch.setattr(settings, "supported_exchanges_csv", "bitget,binance")
    assert (await client.get("/api/exchange/supported")).json() == ["bitget", "binance"]
    r = await client.post("/api/exchange/keys", json={"exchange": "binance", "api_key": "x" * 20, "api_secret": "y" * 20}, headers=auth(user))
    assert r.status_code == 201


async def test_create_rejects_foreign_or_missing_exchange_account(client, db, user, other_user):
    foreign = await make_key(db, other_user)
    r = await client.post(P, json=body(foreign), headers=auth(user))
    assert r.status_code == 404 and r.json()["detail"]["error"] == "exchange_account_not_found"
    r = await client.post(P, json=body(foreign, exchange_account_id=str(uuid.uuid4())), headers=auth(user))
    assert r.status_code == 404


@pytest.mark.parametrize("over", [
    {"amount_usd": 5}, {"amount_usd": 2_000_000}, {"crypto_symbol": "DOGE"}, {"frequency": "hourly"},
    {"blockchain_network": "devnet"}, {"blockchain_network": "LIGHTNING"}, {"wallet_address": "short"},
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
    r = await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "BTC", "label": "cold"}, headers=H)
    assert r.status_code == 201, r.text
    w = r.json()
    assert w["chain"] == "bitcoin" and w["label"] == "cold" and w["verified_at"] and w["blockchain_network"] == "BTC"
    r = await client.post("/api/wallets", json={"address": ETH, "blockchain_network": "ArbitrumOne"}, headers=H)
    assert r.status_code == 201 and r.json()["chain"] == "ethereum"

    # format must match the declared chain
    r = await client.post("/api/wallets", json={"address": BTC_TEST, "blockchain_network": "BTC"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["details"]["detected_network"] == "testnet"
    assert (await client.post("/api/wallets", json={"address": "0xnotanaddress0000000000", "blockchain_network": "ERC20"}, headers=H)).status_code == 422
    assert (await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "mainnet"}, headers=H)).status_code == 422, "legacy network names are gone"
    assert (await client.post("/api/wallets", json={"address": SOL, "blockchain_network": "SOL"}, headers=H)).status_code == 201
    # duplicates
    assert (await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "BTC"}, headers=H)).status_code == 409
    assert (await client.post("/api/wallets", json={"address": BTC_MAIN, "blockchain_network": "BTC"}, headers=auth(other_user))).status_code == 409

    # list + chain filter
    assert {x["address"] for x in (await client.get("/api/wallets", headers=H)).json()} == {BTC_MAIN, ETH, SOL}
    assert [x["address"] for x in (await client.get("/api/wallets", params={"blockchain_network": "arbitrumone"}, headers=H)).json()] == [ETH]
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
    r = await client.post(P, json=body(binance_key, crypto_symbol="ETH", blockchain_network="ERC20", wallet_address=ETH, purchase_type="dca"), headers=auth(user))
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
    """autobuy needs a real wallet; dca does not, but a wallet it does carry must be valid."""
    H = auth(user)
    b = body(binance_key, purchase_type=purchase_type); del b["wallet_address"]
    r = await client.post(P, json=b, headers=H)
    if purchase_type == "autobuy":
        assert r.status_code == 422 and "wallet_address is required" in r.text
        assert (await client.post(P, json=body(binance_key, purchase_type=purchase_type, wallet_address=""), headers=H)).status_code == 422
    else:
        assert r.status_code == 201, r.text
        assert (await client.post(P, json=body(binance_key, purchase_type=purchase_type, wallet_address=""), headers=H)).status_code == 201
    # the wallet must be valid for the coin/network in both modes when given
    r = await client.post(P, json=body(binance_key, purchase_type=purchase_type, wallet_address=BTC_TEST), headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_wallet_address"


@pytest.mark.parametrize("purchase_type", ["autobuy", "dca"])
async def test_burn_and_placeholder_addresses_are_always_rejected(client, user, binance_key, purchase_type):
    H = auth(user)
    for bad in ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "1BitcoinEaterAddressDontSendf59kuE"):
        r = await client.post(P, json=body(binance_key, purchase_type=purchase_type, wallet_address=bad), headers=H)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_address_denylisted", r.text
    r = await client.post(P, json=body(binance_key, crypto_symbol="ETH", purchase_type=purchase_type,
                                       wallet_address="0x000000000000000000000000000000000000DEAD"), headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_address_denylisted"
    r = await client.post("/api/wallets", json={"address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "blockchain_network": "BTC"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_address_denylisted"


async def test_dca_without_wallet_lists_null_and_executes(client, db, user, binance_key):
    H = auth(user)
    b = body(binance_key, purchase_type="dca"); del b["wallet_address"]
    r = await client.post(P, json=b, headers=H)
    assert r.status_code == 201
    pid = r.json()["purchase_id"]
    listed = (await client.get(P, headers=H)).json()
    assert next(x for x in listed if x["id"] == pid)["wallet_address"] is None


async def test_switch_to_withdraw_requires_address_network_and_confirmation(client, db, user, binance_key):
    H = auth(user)
    b = body(binance_key, purchase_type="dca"); del b["wallet_address"]
    pid = (await client.post(P, json=b, headers=H)).json()["purchase_id"]
    # no address
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_address_required"
    # no confirmation
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN, "blockchain_network": "BTC"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_confirmation_mismatch"
    # burn address, even confirmed
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy", "wallet_address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                                             "confirm_wallet_address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "blockchain_network": "BTC"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_address_denylisted"
    # wrong chain for the address
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN,
                                             "confirm_wallet_address": BTC_MAIN, "blockchain_network": "ERC20"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_wallet_address"
    # all good
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN,
                                             "confirm_wallet_address": BTC_MAIN, "blockchain_network": "BTC"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["purchase_type"] == "autobuy" and r.json()["wallet_address"] == BTC_MAIN
    # back to accumulate keeps the wallet; switching to autobuy again still needs confirmation
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "dca"}, headers=H)
    assert r.status_code == 200 and r.json()["purchase_type"] == "dca" and r.json()["wallet_address"] == BTC_MAIN
    r = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN, "blockchain_network": "BTC"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_confirmation_mismatch"


async def test_dca_skips_the_withdrawal_checks_but_not_the_pair_check(client, user, bitget_key):
    # below the BTC-chain minimum withdrawal: refused for autobuy, fine for accumulate
    assert (await client.post(P, json=body(bitget_key, amount_usd=15, purchase_type="autobuy"), headers=auth(user))).status_code == 422
    assert (await client.post(P, json=body(bitget_key, amount_usd=15, purchase_type="dca"), headers=auth(user))).status_code == 201, \
        "nothing is withdrawn, so the withdrawal minimum does not matter"
    # but a pair that does not exist is refused in both modes
    assert (await client.post(P, json=body(bitget_key, crypto_symbol="USDC", blockchain_network="ERC20", wallet_address=ETH,
                                           purchase_type="dca"), headers=auth(user))).status_code == 422


async def test_transactions_expose_tx_type(client, db, user, binance_key):
    p = await make_purchase(db, user, binance_key, purchase_type="dca")
    db.add(PurchaseTransaction(purchase_id=p.id, status="success", tx_type="accumulation", exchange_tx_id="ord-1", executed_at=utcnow()))
    await db.commit()
    out = (await client.get(f"{P}/{p.id}", headers=auth(user))).json()
    assert out["latest_transaction"]["tx_type"] == "accumulation"
    assert out["transactions"][0]["tx_type"] == "accumulation"


async def test_below_minimum_withdrawal_is_rejected_server_side_even_when_posted_directly(client, user, bitget_key, db):
    """A disabled button is not a guard: the raw endpoint, with the legacy
    withdraw_immediately flag and no purchase_type, must refuse a 15 USD BTC
    autobuy and create nothing."""
    from sqlalchemy import func, select
    from api.models import Purchase
    before = (await db.execute(select(func.count(Purchase.id)).where(Purchase.user_id == user.id))).scalar_one()
    raw = {"crypto_symbol": "BTC", "amount_usd": 15, "wallet_address": BTC_MAIN, "blockchain_network": "BTC",
           "frequency": "daily", "exchange_account_id": str(bitget_key.id), "withdraw_immediately": True}
    r = await client.post(P, json=raw, headers=auth(user))
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["error"] == "below_minimum_withdrawal" and d["details"]["amount_usd"] == 15.0 and d["details"]["chain"] == "BTC"
    assert d["details"]["min_withdraw_amount"] == 0.0005 and d["details"]["minimum_usd"] > 15
    after = (await db.execute(select(func.count(Purchase.id)).where(Purchase.user_id == user.id))).scalar_one()
    assert after == before, "nothing was created"
    # the same guard covers switching an existing accumulate purchase to withdraw-immediately
    ok = await client.post(P, json={**raw, "withdraw_immediately": False}, headers=auth(user))
    assert ok.status_code == 201
    pid = ok.json()["purchase_id"]
    sw = await client.put(f"{P}/{pid}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN,
                                             "blockchain_network": "BTC", "confirm_wallet_address": BTC_MAIN}, headers=auth(user))
    assert sw.status_code == 422 and sw.json()["detail"]["error"] == "below_minimum_withdrawal"
    assert (await client.get(f"{P}/{pid}", headers=auth(user))).json()["purchase_type"] == "dca", "still accumulate"


async def test_price_lookup_failure_during_validation_is_503_not_500(client, user, bitget_key, monkeypatch, db):
    """Seen live: Bitget dropped the ticker connection while a 15 USD autobuy was
    being validated and the API answered 500. It must answer 503 with the plain
    message, and create nothing, exactly like the other market-data failures."""
    from sqlalchemy import func, select
    from api.models import Purchase
    from api.services import bitget_market as bm

    class Down:
        def get_ticker(self, symbol):
            raise ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
    monkeypatch.setattr(bm, "_client", lambda: Down())
    monkeypatch.setattr(bm, "live_ask", bm.live_ask_unstubbed)   # the real lookup, against a client that drops the connection
    before = (await db.execute(select(func.count(Purchase.id)).where(Purchase.user_id == user.id))).scalar_one()
    r = await client.post(P, json=body(bitget_key, amount_usd=15, withdraw_immediately=True), headers=auth(user))
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == {"error": "exchange_data_unavailable", "details": {"message": bm.UNAVAILABLE_MESSAGE}}
    assert (await db.execute(select(func.count(Purchase.id)).where(Purchase.user_id == user.id))).scalar_one() == before
