"""End-to-end journeys against the full app (register -> ... -> dashboard),
cross-model cascade behavior, and concurrency isolation."""
import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from api.database import AsyncSessionLocal
from api.models import APIKey, Purchase, PurchaseTransaction, User

PW = "Str0ng!passw0rd"


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def cleanup(db, email):
    u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if u:
        await db.delete(u)
        await db.commit()


async def test_full_journey_to_executed_purchase(db, monkeypatch):
    """register -> login -> KYC -> exchange key -> Auto-Buy purchase -> executor run
    -> history visible in the Transactions API and on the dashboard."""
    from datetime import datetime, timedelta, timezone
    from api.models import Purchase
    from api.tasks import purchase_scheduler as ps
    from tests.test_purchase_events import RichGateway
    email = f"journey-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with fresh_client() as c:
            r = await c.post("/api/auth/register", json={"email": email, "password": PW})
            assert r.status_code == 201
            tokens = (await c.post("/api/auth/login",
                                   json={"email": email, "password": PW})).json()
            H = {"Authorization": f"Bearer {tokens['access_token']}"}

            # KYC: simulate the verified webhook outcome directly in DB
            u = (await db.execute(select(User).where(User.email == email))).scalar_one()
            u.kyc_status = "verified"
            await db.commit()

            r = await c.post("/api/exchange/keys", headers=H,
                             json={"exchange": "binance", "api_key": "AKIAJOURNEYKEY123",
                                   "api_secret": "journeysecret456"})
            assert r.status_code == 201
            key_id = r.json()["id"]
            async with AsyncSessionLocal() as s:
                k = (await s.execute(select(APIKey).where(APIKey.id == uuid.UUID(key_id)))).scalar_one()
                k.verified = True
                await s.commit()

            r = await c.post("/api/v2/purchases", headers=H,
                             json={"crypto_symbol": "BTC", "amount_usd": 100, "frequency": "weekly",
                                   "wallet_address": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
                                   "blockchain_network": "mainnet", "exchange_account_id": key_id,
                                   "purchase_type": "autobuy"})
            assert r.status_code == 201, r.text
            purchase_id = uuid.UUID(r.json()["purchase_id"])

            # make it due, then run the executor (exchange stubbed, dry run)
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as s:
                p = (await s.execute(select(Purchase).where(Purchase.id == purchase_id))).scalar_one()
                p.next_execution = now - timedelta(minutes=2)
                await s.commit()
                counts = await ps.execute_due_purchases(s, now=now, gateway=RichGateway(), dry_run=True)
            assert counts["executed"] == 1

            txs = (await c.get("/api/transactions/", headers=H)).json()
            assert txs["total"] == 1 and txs["items"][0]["status"] == "completed"
            assert txs["items"][0]["order_id"] == "ORD-1" and txs["items"][0]["withdrawal_id"] == "WD-1"
            d = (await c.get("/api/dashboard/", headers=H)).json()
            assert d["holdings"][0]["crypto"] == "BTC" and d["holdings"][0]["total_cost_usdt"] == 100.0
            async with AsyncSessionLocal() as s:
                p = (await s.execute(select(Purchase).where(Purchase.id == purchase_id))).scalar_one()
                assert p.next_execution > now
    finally:
        await cleanup(db, email)


async def test_user_delete_cascades_everything(db, user, binance_key):
    from tests.api.conftest import make_autobuy_purchase, make_history
    p = await make_autobuy_purchase(db, user, key=binance_key)
    tx = await make_history(db, user, purchase=p, amount_crypto=0.001, cost_usd=50.0)
    ids = (user.id, p.id, binance_key.id, tx.id)
    await db.delete(user)
    await db.commit()
    assert (await db.execute(select(User).where(User.id == ids[0]))).scalar_one_or_none() is None
    assert (await db.execute(select(Purchase).where(Purchase.id == ids[1]))).scalar_one_or_none() is None
    assert (await db.execute(select(APIKey).where(APIKey.id == ids[2]))).scalar_one_or_none() is None
    assert (await db.execute(select(PurchaseTransaction).where(
        PurchaseTransaction.id == ids[3]))).scalar_one_or_none() is None


async def test_concurrent_users_are_isolated(db):
    """One executor run serves every user's due purchase; each attempt lands on
    its own purchase with its own amount, and a second concurrent run finds
    the run lock held and does nothing."""
    from api.models import utcnow
    from api.routes.exchange import encrypt_value
    from api.tasks import purchase_scheduler as ps
    from tests.api.conftest import make_autobuy_purchase
    from tests.test_purchase_events import RichGateway

    users = []
    for i in range(2):
        u = User(email=f"conc{i}-{uuid.uuid4().hex[:8]}@example.com",
                 password_hash="x", kyc_status="verified")
        db.add(u)
        await db.flush()
        key = APIKey(user_id=u.id, exchange="binance",
                     encrypted_api_key=encrypt_value(f"key-user-{i}-000000"),
                     encrypted_api_secret=encrypt_value(f"sec-user-{i}-000000"),
                     verified=True)
        db.add(key)
        await db.commit()
        p = await make_autobuy_purchase(db, u, amount_usd=100 + i, key=key, purchase_type="dca",
                                        next_execution=utcnow())
        users.append((u, p))

    async def run():
        async with AsyncSessionLocal() as s:
            return await ps.execute_due_purchases(s, gateway=RichGateway(), dry_run=True)

    try:
        a, b = await asyncio.gather(run(), run())
        assert sorted([a["executed"], b["executed"]]) == [0, 2], "one run does the work, the other is lock-skipped"
        for u, p in users:
            tx = (await db.execute(select(PurchaseTransaction).where(
                PurchaseTransaction.purchase_id == p.id))).scalar_one()
            assert tx.status == "success" and tx.cost_usd == p.amount_usd
    finally:
        for u, _ in users:
            fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
            if fresh:
                await db.delete(fresh)
        await db.commit()
