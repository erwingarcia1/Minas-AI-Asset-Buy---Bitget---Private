import uuid

import pytest

from tests.api.conftest import auth_headers, make_autobuy_purchase, make_history


@pytest.fixture
async def seeded(db, user, binance_key):
    btc = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    eth = await make_autobuy_purchase(db, user, crypto="ETH", amount_usd=150.0, purchase_type="dca", key=binance_key)
    txs = [
        await make_history(db, user, purchase=btc, amount_crypto=0.001, cost_usd=100.0, tx_hash="0xdeadbeef"),
        await make_history(db, user, purchase=btc, amount_crypto=0.002, cost_usd=200.0),
        await make_history(db, user, purchase=eth, amount_crypto=0.05, cost_usd=150.0, status="failed",
                           error_message="boom"),
        await make_history(db, user, purchase=eth, amount_crypto=0.05, cost_usd=150.0, status="pending"),
    ]
    return txs


async def test_list_pagination(client, user, seeded):
    H = auth_headers(user)
    async with client as c:
        full = await c.get("/api/transactions/", headers=H)
        assert full.json()["total"] == 4 and len(full.json()["items"]) == 4
        page = await c.get("/api/transactions/", params={"limit": 2, "offset": 2}, headers=H)
        assert len(page.json()["items"]) == 2 and page.json()["total"] == 4
        bad = await c.get("/api/transactions/", params={"limit": 0}, headers=H)
        assert bad.status_code == 422


async def test_rows_carry_fill_and_withdrawal_details(client, user, seeded):
    async with client as c:
        items = (await c.get("/api/transactions/", headers=auth_headers(user))).json()["items"]
    btc = next(t for t in items if t["id"] == str(seeded[0].id))
    assert btc["crypto"] == "BTC" and btc["purchase_type"] == "autobuy" and btc["tx_type"] == "withdrawal"
    assert btc["status"] == "completed" and btc["cost_usdt"] == 100.0 and btc["amount_crypto"] == 0.001
    assert btc["exchange"] == "binance" and btc["order_id"].startswith("ord-") and btc["binance_order_id"] == btc["order_id"]
    assert btc["exchange_rate"] == pytest.approx(100_000.0)
    assert btc["withdrawal_id"].startswith("wd-") and btc["wallet_address"] and btc["blockchain_network"] == "mainnet"
    assert btc["tx_hash"] == "0xdeadbeef"
    eth = next(t for t in items if t["id"] == str(seeded[2].id))
    assert eth["status"] == "failed" and eth["error_message"] == "boom" and eth["tx_type"] == "accumulation"
    assert eth["wallet_address"] is None and eth["amount_crypto"] == 0.0


async def test_status_filter(client, user, seeded):
    H = auth_headers(user)
    async with client as c:
        done = await c.get("/api/transactions/", params={"status_filter": "completed"}, headers=H)
        assert done.json()["total"] == 2
        bogus = await c.get("/api/transactions/", params={"status_filter": "bogus"}, headers=H)
        assert bogus.status_code == 422


async def test_filter_by_crypto_case_insensitive(client, user, seeded):
    async with client as c:
        r = await c.get("/api/transactions/crypto/btc", headers=auth_headers(user))
    assert len(r.json()) == 2 and all(t["crypto"] == "BTC" for t in r.json())


async def test_filter_by_status_path(client, user, seeded):
    async with client as c:
        r = await c.get("/api/transactions/status/failed", headers=auth_headers(user))
    assert len(r.json()) == 1 and r.json()[0]["error_message"] == "boom"


async def test_stats_summary_math(client, user, seeded):
    async with client as c:
        st = (await c.get("/api/transactions/stats/summary", headers=auth_headers(user))).json()
    assert st == {"total_transactions": 4, "total_invested_usdt": 300.0,
                  "average_purchase_usdt": 150.0, "completed_count": 2,
                  "failed_count": 1, "pending_count": 1}


async def test_get_by_id_scoping(client, db, user, seeded):
    tx_id = seeded[0].id
    from api.models import User
    intruder = User(email=f"intruder-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.add(intruder)
    await db.commit()
    try:
        async with client as c:
            own = await c.get(f"/api/transactions/{tx_id}", headers=auth_headers(user))
            assert own.status_code == 200
            other = await c.get(f"/api/transactions/{tx_id}", headers=auth_headers(intruder))
            assert other.status_code == 404
    finally:
        await db.delete(intruder)
        await db.commit()
