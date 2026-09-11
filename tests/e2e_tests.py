"""End-to-end tests: the complete customer flow through the real ASGI app.

Runs against the same PostgreSQL + Redis the API uses (no HTTP server needed):

    pytest tests/e2e_tests.py -v

Covers register -> login -> profile, Auto-Buy purchase creation -> executor run
(dry-run, exchange stubbed), transaction retrieval + dashboard stats, token
refresh/revocation, rate limiting, and the generated Python SDK driving the
same flow. Every test creates its own `e2e-*@example.com` user and deletes it
(cascading its purchases and history) afterwards, so the local database is
left as found.
Rate-limit counters are keyed by a random per-test client IP so they never
collide with the dev server or with each other.
"""
import os
import random
import sys
import uuid
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("AUTOBUY_DB_POOL", "null")   # pooled conns pin to one event loop
os.environ.setdefault("RESEND_API_KEY", "")        # never send real email from tests

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.database import AsyncSessionLocal
from api.main import app
from api.models import APIKey, Purchase, PurchaseTransaction, User, utcnow
from api.routes.exchange import encrypt_value
from api.services.rate_limit import limiter
from api.tasks import purchase_scheduler as ps

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))

PASSWORD = "Str0ng!Passw0rd-e2e"
BTC_WALLET = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def purchase_body(key_id: str, **over) -> dict:
    body = {"crypto_symbol": "BTC", "amount_usd": 25.0, "frequency": "weekly", "wallet_address": BTC_WALLET,
            "blockchain_network": "mainnet", "exchange_account_id": key_id, "purchase_type": "autobuy"}
    body.update(over)
    return body


# --------------------------------------------------------------------------- helpers

class FakeGateway:
    """Stands in for the exchange gateway so the executor never touches an exchange."""
    def execute(self, account, purchase, dry_run):
        oid = "DRY-e2e" if dry_run else "LIVE-e2e"
        fill = {"order_id": oid, "amount_executed": purchase.amount_usd / 50000.0, "fill_price": 50000.0,
                "cost_usd": purchase.amount_usd, "fee": None, "fee_currency": None}
        if purchase.purchase_type == "dca":
            return {**fill, "exchange_tx_id": oid, "tx_type": "accumulation", "withdrawal_id": None,
                    "withdrawal_fee": None, "withdrawal_fee_currency": None, "tx_hash": None}
        return {**fill, "exchange_tx_id": "DRY-WD-e2e", "tx_type": "withdrawal", "withdrawal_id": "DRY-WD-e2e",
                "withdrawal_fee": None, "withdrawal_fee_currency": None, "tx_hash": None}


def random_ip() -> str:
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def new_email() -> str:
    return f"e2e-{uuid.uuid4().hex[:10]}@example.com"


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- fixtures

@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def cleanup(db):
    """Collects e2e user emails and removes them (with their history) after the test."""
    emails: list[str] = []
    yield emails
    for email in emails:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            continue
        await db.delete(user)  # cascades purchases, history and keys
    await db.commit()


@pytest.fixture
def make_client():
    """ASGI client bound to a random source IP (isolates rate-limit counters)."""
    clients: list[httpx.AsyncClient] = []

    def _make(ip: str | None = None) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(ip or random_ip(), 40000 + random.randint(0, 9999)))
        c = httpx.AsyncClient(transport=transport, base_url="http://e2e")
        clients.append(c)
        return c

    yield _make


@pytest_asyncio.fixture
async def client(make_client):
    c = make_client()
    yield c
    await c.aclose()


async def register_and_login(client: httpx.AsyncClient, cleanup: list[str]) -> tuple[str, dict]:
    email = new_email()
    cleanup.append(email)
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return email, r.json()


async def make_executable(db, email: str) -> tuple[User, APIKey]:
    """KYC-verify the user and attach a (fake) verified Binance key — the executor's gates."""
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    user.kyc_status = "verified"
    key = APIKey(user_id=user.id, exchange="binance",
                 encrypted_api_key=encrypt_value("e2e-api-key-000001"),
                 encrypted_api_secret=encrypt_value("e2e-api-secret-01"), verified=True)
    db.add(key)
    await db.commit()
    return user, key


async def add_key(db, email: str, verified: bool = False) -> APIKey:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    key = APIKey(user_id=user.id, exchange="binance",
                 encrypted_api_key=encrypt_value("e2e-api-key-000001"),
                 encrypted_api_secret=encrypt_value("e2e-api-secret-01"), verified=verified)
    db.add(key)
    await db.commit()
    return key


async def run_executor_for(db, purchase_id: uuid.UUID) -> dict:
    """Make the purchase due, run the Auto-Buy executor in dry-run, return the successful attempt as a dict.

    The dev server's own job may race for the advisory lock; if it wins (and
    records a failure because the key is fake) we re-arm and run again.
    """
    for _ in range(3):
        purchase = (await db.execute(select(Purchase).where(Purchase.id == purchase_id))).scalar_one()
        purchase.status, purchase.failure_count = "active", 0
        purchase.next_execution = utcnow() - timedelta(minutes=1)
        await db.commit()
        await ps.execute_due_purchases(db, gateway=FakeGateway(), dry_run=True)
        db.expire_all()
        tx = (await db.execute(select(PurchaseTransaction).where(
            PurchaseTransaction.purchase_id == purchase_id, PurchaseTransaction.status == "success"))).scalars().first()
        if tx is not None:
            return {"id": str(tx.id), "status": "completed", "order_id": tx.order_id,
                    "cost_usd": tx.cost_usd, "fill_price": tx.fill_price, "withdrawal_id": tx.withdrawal_id}
    pytest.fail("the executor never produced a successful dry-run attempt")


# --------------------------------------------------------------------------- 1. registration -> login -> profile

async def test_register_login_profile(client, cleanup):
    email = new_email()
    cleanup.append(email)

    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["email"] == email and created["kyc_status"] == "pending"
    uuid.UUID(created["id"])

    # duplicate registration is rejected, wrong password is rejected
    assert (await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})).status_code in (400, 409, 422)
    assert (await client.post("/api/auth/login", json={"email": email, "password": "wrong-password"})).status_code == 401

    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert set(tokens) >= {"access_token", "refresh_token", "token_type", "expires_in"}
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] > 0

    # unauthenticated profile access is refused; bearer token works
    assert (await client.get("/api/user/profile")).status_code == 401
    r = await client.get("/api/user/profile", headers=bearer(tokens["access_token"]))
    assert r.status_code == 200, r.text
    profile = r.json()
    assert profile["id"] == created["id"] and profile["email"] == email


# --------------------------------------------------------------------------- 2. purchase -> scheduler (dry run)

async def test_purchase_creation_and_executor_dry_run(client, db, cleanup):
    email, tokens = await register_and_login(client, cleanup)
    auth = bearer(tokens["access_token"])
    key = await add_key(db, email, verified=True)

    # validation guards: below the minimum amount; wallet must match the coin
    r = await client.post("/api/v2/purchases", json=purchase_body(str(key.id), amount_usd=1), headers=auth)
    assert r.status_code == 422
    r = await client.post("/api/v2/purchases", json=purchase_body(str(key.id), wallet_address="0x52908400098527886E0F7030069857D2E4169EE7"), headers=auth)
    assert r.status_code == 422

    r = await client.post("/api/v2/purchases", json=purchase_body(str(key.id)), headers=auth)
    assert r.status_code == 201, r.text
    purchase_id = uuid.UUID(r.json()["purchase_id"])
    assert r.json()["status"] == "active"
    r = await client.get(f"/api/v2/purchases/{purchase_id}", headers=auth)
    assert r.status_code == 200
    purchase = r.json()
    assert purchase["crypto_symbol"] == "BTC" and purchase["amount_usd"] == 25.0 and purchase["frequency"] == "weekly"
    first_due = purchase["next_execution"]

    # executor gate: unverified KYC must not buy -> the run skips the purchase, records nothing
    p = (await db.execute(select(Purchase).where(Purchase.id == purchase_id))).scalar_one()
    p.next_execution = utcnow() - timedelta(minutes=1)
    await db.commit()
    counts = await ps.execute_due_purchases(db, gateway=FakeGateway(), dry_run=True)
    assert counts["skipped"] >= 1 and counts["executed"] == 0
    db.expire_all()
    assert (await db.execute(select(PurchaseTransaction).where(
        PurchaseTransaction.purchase_id == purchase_id))).scalars().all() == []

    # now verified: dry-run execution succeeds and the schedule advances one week
    await make_executable(db, email)
    tx = await run_executor_for(db, purchase_id)
    assert tx["status"] == "completed" and tx["order_id"] == "DRY-e2e" and tx["withdrawal_id"] == "DRY-WD-e2e"
    assert tx["cost_usd"] == 25.0 and tx["fill_price"] == 50000.0

    r = await client.get(f"/api/v2/purchases/{purchase_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["next_execution"] > first_due
    assert r.json()["transactions"][0]["status"] == "success"

    # pause + list round-trip
    r = await client.put(f"/api/v2/purchases/{purchase_id}", json={"status": "paused"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = await client.get("/api/v2/purchases", headers=auth)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [str(purchase_id)]


# --------------------------------------------------------------------------- 3. transactions + dashboard

async def test_transactions_and_dashboard_stats(client, db, cleanup):
    email, tokens = await register_and_login(client, cleanup)
    auth = bearer(tokens["access_token"])

    # empty state first
    r = await client.get("/api/transactions/", headers=auth)
    assert r.status_code == 200 and r.json() == {"items": [], "total": 0, "limit": r.json()["limit"], "offset": 0}
    r = await client.get("/api/dashboard/stats", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"total_invested_usdt": 0, "transaction_count": 0, "active_purchases": 0, "kyc_status": "pending"}

    _, key = await make_executable(db, email)
    r = await client.post("/api/v2/purchases", json=purchase_body(str(key.id)), headers=auth)
    assert r.status_code == 201, r.text
    purchase_id = uuid.UUID(r.json()["purchase_id"])
    tx = await run_executor_for(db, purchase_id)

    # list + pagination
    r = await client.get("/api/transactions/", params={"limit": 10, "offset": 0}, headers=auth)
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == 1 and page["limit"] == 10 and len(page["items"]) == 1
    item = page["items"][0]
    assert item["id"] == tx["id"] and item["status"] == "completed" and item["cost_usdt"] == 25.0
    assert item["purchase_id"] == str(purchase_id) and item["tx_type"] == "withdrawal"
    assert item["wallet_address"] == BTC_WALLET and item["blockchain_network"] == "mainnet"
    assert item["exchange"] == "binance" and item["order_id"] == "DRY-e2e"
    assert (await client.get("/api/transactions/", params={"limit": 10, "offset": 5}, headers=auth)).json()["items"] == []

    # single, by crypto, by status, summary
    r = await client.get(f"/api/transactions/{tx['id']}", headers=auth)
    assert r.status_code == 200 and r.json()["binance_order_id"] == "DRY-e2e"
    assert [t["id"] for t in (await client.get("/api/transactions/crypto/BTC", headers=auth)).json()] == [tx["id"]]
    assert [t["id"] for t in (await client.get("/api/transactions/status/completed", headers=auth)).json()] == [tx["id"]]
    assert (await client.get("/api/transactions/status/failed", headers=auth)).json() == []
    r = await client.get("/api/transactions/stats/summary", headers=auth)
    assert r.status_code == 200
    summary = r.json()
    assert summary["total_transactions"] == 1 and summary["total_invested_usdt"] == 25.0
    assert summary["average_purchase_usdt"] == 25.0

    # dashboard reflects the executed purchase
    r = await client.get("/api/dashboard/stats", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"total_invested_usdt": 25.0, "transaction_count": 1, "active_purchases": 1, "kyc_status": "verified"}
    r = await client.get("/api/dashboard/", headers=auth)
    assert r.status_code == 200
    dash = r.json()
    assert set(dash) >= {"holdings", "active_purchases", "upcoming_purchases", "monthly_average_usdt"}
    assert [h["crypto"] for h in dash["holdings"]] == ["BTC"]
    assert dash["holdings"][0]["total_cost_usdt"] == 25.0
    assert [p["id"] for p in dash["active_purchases"]] == [str(purchase_id)]
    assert [u["crypto"] for u in dash["upcoming_purchases"]] == ["BTC"]
    assert dash["monthly_average_usdt"] > 0

    # the tax CSV carries the fill and withdrawal details
    year = utcnow().year
    r = await client.get("/api/reports/tax-summary", params={"year": year, "format": "csv"}, headers=auth)
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("datetime_utc,crypto,quantity,fill_price_usd,usd_amount")
    assert len(lines) == 2 and "DRY-e2e" in lines[1] and BTC_WALLET in lines[1] and "binance" in lines[1]

    # a different user cannot see it
    _, other = await register_and_login(client, cleanup)
    assert (await client.get(f"/api/transactions/{tx['id']}", headers=bearer(other["access_token"]))).status_code == 404


# --------------------------------------------------------------------------- 4. token refresh

async def test_token_refresh_and_revocation(client, cleanup):
    email, tokens = await register_and_login(client, cleanup)
    profile = "/api/user/profile"

    # tokens are not interchangeable
    assert (await client.get(profile, headers=bearer(tokens["refresh_token"]))).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": "not-a-jwt"})).status_code == 401

    r = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]   # refresh tokens carry a jti and rotate
    assert fresh["expires_in"] > 0                               # access tokens are deterministic per second
    r = await client.get(profile, headers=bearer(fresh["access_token"]))
    assert r.status_code == 200 and r.json()["email"] == email

    # logout revokes the refresh token immediately; the access token still expires naturally
    r = await client.post("/api/auth/logout", json={"refresh_token": fresh["refresh_token"]})
    assert r.status_code == 204, r.text
    assert (await client.post("/api/auth/refresh", json={"refresh_token": fresh["refresh_token"]})).status_code == 401
    assert (await client.get(profile, headers=bearer(fresh["access_token"]))).status_code == 200

    # password change invalidates every token issued before it
    r = await client.post("/api/auth/change-password",
                          json={"current_password": PASSWORD, "new_password": PASSWORD + "-2"},
                          headers=bearer(fresh["access_token"]))
    assert r.status_code == 204, r.text
    assert (await client.get(profile, headers=bearer(fresh["access_token"]))).status_code == 401
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD + "-2"})
    assert r.status_code == 200


# --------------------------------------------------------------------------- 5. rate limiting

async def test_rate_limiting(make_client, cleanup, monkeypatch):
    try:
        from api.services.redis_client import get_redis
        await get_redis().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Redis unavailable: {exc}")
    monkeypatch.setattr(limiter, "enabled", True)  # tests/api/conftest disables it for the unit suite

    ip = random_ip()
    client = make_client(ip)
    try:
        email, _ = await register_and_login(client, cleanup)  # 1 register + 1 login from `ip`
        creds = {"email": email, "password": PASSWORD}

        # login is 5/minute per IP: four more succeed, the sixth is throttled
        statuses = []
        for _ in range(4):
            r = await client.post("/api/auth/login", json=creds)
            statuses.append(r.status_code)
        assert statuses == [200] * 4, statuses
        assert r.headers.get("x-ratelimit-limit") == "5"
        assert r.headers.get("x-ratelimit-remaining") == "0"

        r = await client.post("/api/auth/login", json=creds)
        assert r.status_code == 429, r.text
        assert int(r.headers["retry-after"]) >= 1
        assert "5 per 1 minute" in r.json()["error"]

        # limits are per client, not global: a different IP is unaffected
        other = make_client(random_ip())
        try:
            assert (await other.post("/api/auth/login", json=creds)).status_code == 200
        finally:
            await other.aclose()
    finally:
        await client.aclose()


async def test_rate_limit_per_user_on_authenticated_routes(make_client, cleanup, monkeypatch):
    try:
        from api.services.redis_client import get_redis
        await get_redis().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Redis unavailable: {exc}")
    monkeypatch.setattr(limiter, "enabled", True)

    client = make_client(random_ip())
    try:
        _, tokens = await register_and_login(client, cleanup)
        auth = bearer(tokens["access_token"])
        codes = [(await client.get("/api/dashboard/", headers=auth)).status_code for _ in range(11)]
        assert codes[:10] == [200] * 10 and codes[10] == 429, codes

        # another user from the same IP still has their own budget
        _, tokens2 = await register_and_login(client, cleanup)
        assert (await client.get("/api/dashboard/", headers=bearer(tokens2["access_token"]))).status_code == 200
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- 6. generated Python SDK against the real app

async def test_python_sdk_end_to_end(db, cleanup):
    from autobuy_client import ApiError, AsyncAutoBuyClient, ValidationError

    email = new_email()
    cleanup.append(email)
    changes = []
    sdk = AsyncAutoBuyClient("http://e2e", transport=httpx.ASGITransport(app=app, client=(random_ip(), 4321)),
                             on_tokens_changed=changes.append)
    async with sdk:
        assert (await sdk.system.health_check())["status"] == "healthy"
        user = await sdk.register(email, PASSWORD)
        assert user["email"] == email
        await sdk.login(email, PASSWORD)
        assert sdk.tokens and changes[-1]["access_token"] == sdk.tokens["access_token"]
        assert (await sdk.me())["id"] == user["id"]

        _, key = await make_executable(db, email)
        with pytest.raises(ValidationError) as ei:
            await sdk.auto_buy.create_purchase(purchase_body(str(key.id), amount_usd=1))
        assert any("amount_usd" in e.get("loc", []) for e in ei.value.errors)

        purchase = await sdk.create_purchase(purchase_body(str(key.id)))
        tx = await run_executor_for(db, uuid.UUID(purchase["purchase_id"]))

        page = await sdk.transactions.list_transactions(limit=5)
        assert page["total"] == 1 and page["items"][0]["id"] == tx["id"]
        stats = await sdk.dashboard.quick_stats()
        assert stats["transaction_count"] == 1 and stats["total_invested_usdt"] == 25.0

        # explicit refresh rotates the pair; automatic refresh recovers from a stale access token
        old = dict(sdk.tokens)
        await sdk.refresh_tokens()
        assert sdk.tokens["refresh_token"] != old["refresh_token"]
        sdk.set_tokens({"access_token": "stale.token.value", "refresh_token": sdk.tokens["refresh_token"]})
        assert (await sdk.me())["email"] == email
        assert sdk.tokens["access_token"] != "stale.token.value"

        with pytest.raises(ApiError) as ei:
            await sdk.auto_buy.get_purchase(str(uuid.uuid4()))
        assert ei.value.status == 404

        await sdk.logout()
        assert sdk.tokens is None and changes[-1] is None
