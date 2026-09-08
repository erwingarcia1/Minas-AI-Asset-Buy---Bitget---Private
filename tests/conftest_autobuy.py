"""Shared fixtures for the Auto-Buy purchase tests (imported explicitly, not a pytest conftest)."""
import os
import uuid

os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
os.environ.setdefault("AUTOBUY_DB_POOL", "null")
os.environ.setdefault("RESEND_API_KEY", "")

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from api.auth import create_access_token
from api.database import AsyncSessionLocal, Base, engine
from api.models import APIKey, Purchase, User
from api.routes.exchange import encrypt_value

BTC_MAIN = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BTC_TEST = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn"
ETH = "0x52908400098527886E0F7030069857D2E4169EE7"
SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


@pytest_asyncio.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as session:
        yield session


async def _make_user(db, **overrides) -> User:
    u = User(email=f"ab-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", kyc_status="verified", **overrides)
    db.add(u)
    await db.commit()
    return u


async def _cleanup_user(db, u: User) -> None:
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)  # cascades api_keys, wallets, purchases -> purchase_transactions
        await db.commit()


@pytest_asyncio.fixture
async def user(db):
    u = await _make_user(db)
    yield u
    await _cleanup_user(db, u)


@pytest_asyncio.fixture
async def other_user(db):
    u = await _make_user(db)
    yield u
    await _cleanup_user(db, u)


async def make_key(db, user: User, exchange: str = "binance") -> APIKey:
    k = APIKey(user_id=user.id, exchange=exchange, encrypted_api_key=encrypt_value("ab-key-000001"),
               encrypted_api_secret=encrypt_value("ab-secret-01"), verified=True)
    db.add(k)
    await db.commit()
    return k


@pytest_asyncio.fixture
async def binance_key(db, user):
    return await make_key(db, user, "binance")


async def make_purchase(db, user: User, key: APIKey, **overrides) -> Purchase:
    from api.models import utcnow
    fields = dict(user_id=user.id, exchange_account_id=key.id, crypto_symbol="BTC", amount_usd=50.0,
                  frequency="daily", wallet_address=BTC_MAIN, blockchain_network="mainnet",
                  status="active", failure_count=0, next_execution=utcnow())
    fields.update(overrides)
    p = Purchase(**fields)
    db.add(p)
    await db.commit()
    return p


def auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}


@pytest_asyncio.fixture
async def client():
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
