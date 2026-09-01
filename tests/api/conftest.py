import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from api.database import AsyncSessionLocal, engine, Base
from api.models import APIKey, RecurringPurchase, Transaction, User, utcnow
from api.routes.exchange import encrypt_value


@pytest_asyncio.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def user(db):
    u = User(email=f"svc-{uuid.uuid4().hex[:8]}@example.com",
             password_hash="x", kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    # cascade removes the user's purchases/keys/transactions
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest_asyncio.fixture
async def purchase(db, user):
    p = RecurringPurchase(user_id=user.id, crypto="BTC", amount_usdt=100.0,
                          frequency="weekly", next_purchase_date=utcnow())
    db.add(p)
    await db.commit()
    return p


@pytest_asyncio.fixture
async def binance_key(db, user):
    k = APIKey(user_id=user.id, exchange="binance",
               encrypted_api_key=encrypt_value("test-api-key-000001"),
               encrypted_api_secret=encrypt_value("test-api-secret-01"),
               verified=True)
    db.add(k)
    await db.commit()
    return k


def auth_headers(user) -> dict:
    from api.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}


@pytest.fixture
def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
