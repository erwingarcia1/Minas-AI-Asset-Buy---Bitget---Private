import uuid

import pytest
from sqlalchemy import select

from api.models import RecurringPurchase, Transaction, utcnow
from tests.api.conftest import auth_headers


async def test_create_and_defaults(client, db, user):
    async with client as c:
        r = await c.post("/api/purchases/", headers=auth_headers(user),
                         json={"crypto": "sol", "amount_usdt": 25, "frequency": "Daily"})
    assert r.status_code == 201
    body = r.json()
    assert body["crypto"] == "SOL" and body["frequency"] == "daily" and body["enabled"]
    assert body["next_purchase_date"] > body["created_at"]


async def test_create_invalid_cases(client, user):
    H = auth_headers(user)
    cases = [
        {"crypto": "BTC", "amount_usdt": 5, "frequency": "weekly"},          # below min
        {"crypto": "XYZ", "amount_usdt": 50, "frequency": "weekly"},         # not whitelisted
        {"crypto": "BTC", "amount_usdt": 50, "frequency": "hourly"},         # bad frequency
        {"crypto": "BTC", "amount_usdt": 50, "frequency": "weekly",          # ETH addr on BTC
         "withdrawal_address": "0x52908400098527886E0F7030069857D2E4169EE7"},
    ]
    async with client as c:
        for payload in cases:
            assert (await c.post("/api/purchases/", headers=H, json=payload)).status_code == 422, payload


async def test_update_and_toggle(client, db, user, purchase):
    H = auth_headers(user)
    async with client as c:
        r = await c.put(f"/api/purchases/{purchase.id}", headers=H,
                        json={"amount_usdt": 250, "frequency": "monthly"})
        assert r.status_code == 200
        assert r.json()["amount_usdt"] == 250 and r.json()["frequency"] == "monthly"
        assert r.json()["created_at"] == purchase.created_at.isoformat().replace("+00:00", "Z")
        t1 = await c.patch(f"/api/purchases/{purchase.id}/toggle", headers=H)
        assert t1.json()["enabled"] is False
        t2 = await c.patch(f"/api/purchases/{purchase.id}/toggle", headers=H)
        assert t2.json()["enabled"] is True


async def test_update_address_checked_against_final_crypto(client, user, purchase):
    async with client as c:
        r = await c.put(f"/api/purchases/{purchase.id}", headers=auth_headers(user),
                        json={"withdrawal_address": "0x52908400098527886E0F7030069857D2E4169EE7"})
    assert r.status_code == 422, "ETH address on the existing BTC schedule must fail"


async def test_delete_preserves_transactions(client, db, user, purchase):
    tx = Transaction(user_id=user.id, recurring_purchase_id=purchase.id, crypto="BTC",
                     amount_crypto=0.001, cost_usdt=50.0, status="completed")
    db.add(tx)
    await db.commit()
    tx_id = tx.id
    async with client as c:
        r = await c.delete(f"/api/purchases/{purchase.id}", headers=auth_headers(user))
    assert r.status_code == 204
    assert (await db.execute(select(RecurringPurchase).where(
        RecurringPurchase.id == purchase.id))).scalar_one_or_none() is None
    survivor = (await db.execute(select(Transaction).where(
        Transaction.id == tx_id))).scalar_one_or_none()
    assert survivor is not None
    await db.refresh(survivor)  # DELETE ran in the API's session; refresh past the identity map
    assert survivor.recurring_purchase_id is None


async def test_cross_user_404(client, db, user, purchase):
    from api.models import User
    intruder = User(email=f"intruder-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.add(intruder)
    await db.commit()
    try:
        H = auth_headers(intruder)
        async with client as c:
            for method, url in [("get", f"/api/purchases/{purchase.id}"),
                                ("delete", f"/api/purchases/{purchase.id}"),
                                ("patch", f"/api/purchases/{purchase.id}/toggle")]:
                assert (await getattr(c, method)(url, headers=H)).status_code == 404
    finally:
        await db.delete(intruder)
        await db.commit()
