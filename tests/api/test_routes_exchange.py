import uuid

import pytest
from sqlalchemy import select

from api.models import APIKey
from api.routes.exchange import decrypt_value, encrypt_value, mask_api_key
from tests.api.conftest import auth_headers


def test_fernet_round_trip():
    secret = "super-secret-api-key-123"
    token = encrypt_value(secret)
    assert token != secret and secret not in token
    assert decrypt_value(token) == secret


def test_mask_api_key():
    assert mask_api_key("AKIAEXAMPLEKEY12345") == "AKIA...2345"
    assert mask_api_key("shortkey") == "********"
    assert mask_api_key("ab") == "**"


async def test_add_key_stores_encrypted(client, db, user):
    async with client as c:
        r = await c.post("/api/exchange/keys", headers=auth_headers(user),
                         json={"exchange": "binance",
                               "api_key": "AKIAEXAMPLEKEY12345",
                               "api_secret": "sekritsekritsekrit"})
    assert r.status_code == 201
    assert r.json()["api_key_masked"] == "AKIA...2345"
    row = (await db.execute(select(APIKey).where(APIKey.user_id == user.id))).scalar_one()
    assert "AKIAEXAMPLEKEY12345" not in row.encrypted_api_key
    assert decrypt_value(row.encrypted_api_key) == "AKIAEXAMPLEKEY12345"


async def test_upsert_resets_verified(client, db, user, binance_key):
    assert binance_key.verified is True
    async with client as c:
        r = await c.post("/api/exchange/keys", headers=auth_headers(user),
                         json={"exchange": "binance",
                               "api_key": "NEWKEYVALUE99887766",
                               "api_secret": "newsecretvalue123"})
    assert r.status_code == 201 and r.json()["id"] == str(binance_key.id)
    await db.refresh(binance_key)
    assert binance_key.verified is False, "credential change must force re-verification"


async def test_unsupported_exchange_422(client, user):
    async with client as c:
        r = await c.post("/api/exchange/keys", headers=auth_headers(user),
                         json={"exchange": "kraken", "api_key": "x" * 20,
                               "api_secret": "y" * 20})
    assert r.status_code == 422


async def test_delete_other_users_key_404(client, db, user, binance_key):
    from api.models import User
    intruder = User(email=f"intruder-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.add(intruder)
    await db.commit()
    try:
        async with client as c:
            r = await c.delete(f"/api/exchange/keys/{binance_key.id}",
                               headers=auth_headers(intruder))
        assert r.status_code == 404
        assert (await db.execute(select(APIKey).where(
            APIKey.id == binance_key.id))).scalar_one_or_none() is not None
    finally:
        await db.delete(intruder)
        await db.commit()
