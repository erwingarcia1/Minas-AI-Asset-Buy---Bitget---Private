"""v0.7.7.2: a purchase on an unverified or flagged key is refused at creation and at resume, with the key's message."""
import pytest

from api.auth import create_access_token
from api.services import key_verification as kv
from tests.conftest_autobuy import make_key, make_purchase

pytestmark = pytest.mark.asyncio


def H(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}


BODY = {"crypto_symbol": "XRP", "amount_usd": 10, "frequency": "weekly", "purchase_type": "dca"}


async def test_create_on_a_flagged_unified_key_is_refused_with_the_keys_message(db, user, client):
    key = await make_key(db, user, exchange="bitget")
    key.verified, key.verification_status, key.verification_error = False, "failed", kv.unified_message()
    await db.commit()
    r = await client.post("/api/v2/purchases", json={**BODY, "exchange_account_id": str(key.id)}, headers=H(user))
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["error"] == "exchange_key_unverified" and d["details"]["message"] == kv.unified_message()
    assert d["details"]["message"].startswith("This Bitget account uses Unified Trading mode")


async def test_create_on_a_never_verified_key_is_refused_with_the_generic_message(db, user, client):
    key = await make_key(db, user, exchange="bitget")
    key.verified, key.verification_status = False, "pending"
    await db.commit()
    r = await client.post("/api/v2/purchases", json={**BODY, "exchange_account_id": str(key.id)}, headers=H(user))
    assert r.status_code == 409 and "not verified" in r.json()["detail"]["details"]["message"]


async def test_resume_on_a_flagged_key_is_refused_and_the_purchase_stays_paused(db, user, client):
    key = await make_key(db, user, exchange="bitget")
    p = await make_purchase(db, user, key, crypto_symbol="XRP", status="paused")
    key.verified, key.verification_status, key.verification_error = False, "failed", kv.unified_message()
    await db.commit()
    r = await client.put(f"/api/v2/purchases/{p.id}", json={"status": "active"}, headers=H(user))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "exchange_key_unverified"
    assert r.json()["detail"]["details"]["message"] == kv.unified_message()
    await db.refresh(p); assert p.status == "paused"
    # pausing, or editing the amount, is still allowed on such a key
    r2 = await client.put(f"/api/v2/purchases/{p.id}", json={"amount_usd": 12}, headers=H(user))
    assert r2.status_code == 200 and r2.json()["amount_usd"] == 12


async def test_create_and_resume_on_a_verified_key_still_work(db, user, client):
    key = await make_key(db, user, exchange="bitget")
    key.verified, key.verification_status = True, "verified"
    await db.commit()
    r = await client.post("/api/v2/purchases", json={**BODY, "exchange_account_id": str(key.id)}, headers=H(user))
    assert r.status_code == 201, r.text
    pid = r.json()["purchase_id"]
    assert (await client.put(f"/api/v2/purchases/{pid}", json={"status": "paused"}, headers=H(user))).status_code == 200
    assert (await client.put(f"/api/v2/purchases/{pid}", json={"status": "active"}, headers=H(user))).status_code == 200
