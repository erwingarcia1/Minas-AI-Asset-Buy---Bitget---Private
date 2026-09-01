import uuid
from datetime import timedelta

import pytest

from api.models import Transaction, utcnow
from tests.api.conftest import auth_headers


@pytest.fixture
async def seeded(db, user):
    txs = [
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.001,
                    cost_usdt=100.0, status="completed"),
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.002,
                    cost_usdt=200.0, status="completed"),
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.05,
                    cost_usdt=150.0, status="failed", error_message="boom"),
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.05,
                    cost_usdt=150.0, status="pending"),
    ]
    db.add_all(txs)
    await db.commit()
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
