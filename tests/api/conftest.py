"""Fixtures for tests/api/: HTTP-level tests of one router or service.

Rate limiting is off, the DB pool is disabled (pooled asyncpg connections pin
to one event loop) and no real email can be sent. See the docstring in
tests/conftest.py for the tests/ vs tests/api/ rule.
"""
import os
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
os.environ.setdefault("AUTOBUY_DB_POOL", "null")  # pooled conns pin to one event loop
os.environ.setdefault("RESEND_API_KEY", "")  # never attempt real email sends from tests

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from api.database import AsyncSessionLocal, engine, Base
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow
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


# ------------------------------------------------------------------ Auto-Buy history helpers

WALLETS = {"BTC": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", "ETH": "0x52908400098527886E0F7030069857D2E4169EE7",
           "SOL": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"}


async def make_autobuy_purchase(db, user, crypto="BTC", amount_usd=50.0, purchase_type="autobuy",
                                key=None, **overrides) -> Purchase:
    fields = dict(user_id=user.id, exchange_account_id=key.id if key is not None else None,
                  crypto_symbol=crypto, amount_usd=amount_usd, frequency="weekly",
                  wallet_address=WALLETS.get(crypto, "0x52908400098527886E0F7030069857D2E4169EE7"),
                  blockchain_network={"BTC": "BTC", "SOL": "SOL"}.get(crypto, "ERC20"),
                  purchase_type=purchase_type, status="active",
                  next_execution=utcnow())
    fields.update(overrides)
    p = Purchase(**fields)
    db.add(p)
    await db.commit()
    return p


async def make_history(db, user, crypto="BTC", amount_crypto=0.001, cost_usd=50.0, status="completed",
                       purchase=None, purchase_type="autobuy", key=None, created_at=None, with_withdrawal=False,
                       withdrawal_id=None, withdrawal_fee=None, withdrawal_fee_currency=None, tx_hash=None,
                       withdrawal_chain=None, **tx_fields) -> PurchaseTransaction:
    """One BUY attempt in purchase_transactions (the history source of truth).
    `status` uses the API vocabulary (completed | failed | pending). With
    `with_withdrawal=True` a successful buy also gets its child withdrawal row
    (tx_type withdrawal, parent_transaction_id -> the buy), reachable as
    `buy.withdrawal`."""
    from api.services.history import from_api_status
    if purchase is None:
        purchase = await make_autobuy_purchase(db, user, crypto=crypto, amount_usd=cost_usd,
                                               purchase_type=purchase_type, key=key)
    db_status = from_api_status(status)
    fields = dict(purchase_id=purchase.id, status=db_status, tx_type="buy",
                  amount_executed=amount_crypto if db_status == "success" else 0.0,
                  cost_usd=cost_usd, executed_at=created_at or utcnow(),
                  client_oid=f"ab-test-{uuid.uuid4().hex[:16]}")
    if db_status == "success":
        fields.update(order_id=f"ord-{uuid.uuid4().hex[:8]}",
                      fill_price=(cost_usd / amount_crypto) if (amount_crypto and cost_usd is not None) else None)
    fields.update(tx_fields)
    tx = PurchaseTransaction(**fields)
    db.add(tx)
    await db.commit()
    if created_at is not None:
        tx.created_at = created_at
        await db.commit()
    tx.withdrawal = None
    if with_withdrawal and db_status == "success":
        wd = PurchaseTransaction(purchase_id=purchase.id, status="success", tx_type="withdrawal",
                                 parent_transaction_id=tx.id, amount_executed=amount_crypto,
                                 withdrawal_id=withdrawal_id or f"wd-{uuid.uuid4().hex[:8]}",
                                 exchange_tx_id=withdrawal_id or f"wd-{uuid.uuid4().hex[:8]}",
                                 withdrawal_fee=withdrawal_fee, withdrawal_fee_currency=withdrawal_fee_currency,
                                 tx_hash=tx_hash, withdrawal_chain=withdrawal_chain or purchase.blockchain_network,
                                 executed_at=created_at or utcnow(), client_oid=f"abw-test-{uuid.uuid4().hex[:16]}")
        db.add(wd)
        await db.commit()
        if created_at is not None:
            wd.created_at = created_at
            await db.commit()
        tx.withdrawal = wd
    return tx
