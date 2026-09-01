import json

import httpx
import pytest
from sqlalchemy import select

import api.services.webhook_service as wh_mod
from api.models import RecurringPurchase, WebhookEndpoint, utcnow
from api.services import purchase_service as ps_mod
from api.services.webhook_service import deliver_one, sign
from tests.api.conftest import auth_headers
from tests.api.test_purchase_service import FakeBinance


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------- exchanges

class VenueRecorder:
    """Fake ExchangeClient capturing which venue and symbol were used."""
    calls = []

    def __init__(self, api_key, api_secret, passphrase="", exchange="binance"):
        self.exchange = exchange
        self.passphrase = passphrase

    def spot_symbol(self, base, quote="USDT"):
        return f"{base}-{quote}" if self.exchange == "okx" else f"{base}{quote}"

    def execute_order(self, symbol, usdt_amount, dry_run=True, client_oid=None):
        VenueRecorder.calls.append((self.exchange, symbol, self.passphrase))
        return {"order_id": "1", "amount_crypto": usdt_amount / 100.0,
                "exchange_rate": 100.0, "cost_usdt": usdt_amount, "dry_run": dry_run}


async def test_executor_routes_by_purchase_exchange(db, user, monkeypatch):
    from api.routes.exchange import encrypt_value
    from api.models import APIKey
    from api.services.purchase_service import PurchaseService
    monkeypatch.setattr(ps_mod, "BinanceClient", VenueRecorder)
    VenueRecorder.calls = []
    db.add(APIKey(user_id=user.id, exchange="okx",
                  encrypted_api_key=encrypt_value("okx-key-0000000000"),
                  encrypted_api_secret=encrypt_value("okx-sec-0000000000"),
                  encrypted_passphrase=encrypt_value("okx-passphrase"),
                  verified=True))
    p = RecurringPurchase(user_id=user.id, crypto="BTC", exchange="okx",
                          amount_usdt=100, frequency="weekly",
                          next_purchase_date=utcnow())
    db.add(p)
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, p.id)
    assert tx.status == "completed"
    assert VenueRecorder.calls == [("okx", "BTC-USDT", "okx-passphrase")], \
        "OKX venue, OKX symbol format, decrypted passphrase"


async def test_missing_venue_key_fails_cleanly(db, user, binance_key, monkeypatch):
    from api.services.purchase_service import PurchaseService
    monkeypatch.setattr(ps_mod, "BinanceClient", VenueRecorder)
    p = RecurringPurchase(user_id=user.id, crypto="BTC", exchange="bybit",
                          amount_usdt=50, frequency="weekly",
                          next_purchase_date=utcnow())
    db.add(p)
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, p.id)
    assert tx.status == "failed" and "no bybit API key" in tx.error_message


async def test_okx_key_requires_passphrase(db, user):
    async with client() as c:
        r = await c.post("/api/exchange/keys", headers=auth_headers(user),
                         json={"exchange": "okx", "api_key": "x" * 20,
                               "api_secret": "y" * 20})
        assert r.status_code == 422 and "passphrase" in r.text
        ok = await c.post("/api/exchange/keys", headers=auth_headers(user),
                          json={"exchange": "okx", "api_key": "x" * 20,
                                "api_secret": "y" * 20, "passphrase": "p" * 10})
        assert ok.status_code == 201


async def test_purchase_create_validates_exchange(db, user):
    async with client() as c:
        bad = await c.post("/api/purchases/", headers=auth_headers(user),
                           json={"crypto": "BTC", "amount_usdt": 50,
                                 "frequency": "weekly", "exchange": "kraken"})
        assert bad.status_code == 422
        ok = await c.post("/api/purchases/", headers=auth_headers(user),
                          json={"crypto": "BTC", "amount_usdt": 50,
                                "frequency": "weekly", "exchange": "bitget"})
        assert ok.status_code == 201 and ok.json()["exchange"] == "bitget"


# ---------------------------------------------------------------- webhooks

@pytest.fixture
def capture_delivery(monkeypatch):
    delivered = []
    state = {"status": 200}

    def handler(request):
        delivered.append(request)
        return httpx.Response(state["status"])

    real = httpx.AsyncClient
    monkeypatch.setattr(wh_mod, "httpx", type("H", (), {
        "AsyncClient": lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        "HTTPError": httpx.HTTPError, "Response": httpx.Response}))
    monkeypatch.setattr(wh_mod.asyncio, "sleep", _instant_sleep)
    return delivered, state


async def _instant_sleep(_):
    return None


async def test_webhook_crud_ping_and_signature(db, user, capture_delivery):
    delivered, state = capture_delivery
    H = auth_headers(user)
    async with client() as c:
        bad = await c.post("/api/integrations/webhooks", headers=H,
                           json={"url": "http://insecure.example/hook"})
        assert bad.status_code == 422

        created = await c.post("/api/integrations/webhooks", headers=H,
                               json={"url": "https://example.com/hook"})
        assert created.status_code == 201
        secret = created.json()["secret"]
        hook_id = created.json()["id"]

        listed = (await c.get("/api/integrations/webhooks", headers=H)).json()
        assert len(listed) == 1 and "secret" not in listed[0]

        pinged = await c.post(f"/api/integrations/webhooks/{hook_id}/ping", headers=H)
        assert pinged.json()["last_status"] == "200"

    req = delivered[0]
    body = req.content
    assert req.headers["x-autobuy-signature"] == sign(secret, body)
    assert json.loads(body)["event"] == "ping"


async def test_webhook_delivery_retries_5xx_not_4xx(db, user, capture_delivery):
    delivered, state = capture_delivery
    ep = WebhookEndpoint(user_id=user.id, url="https://example.com/h", secret="s")
    state["status"] = 503
    status = await deliver_one(ep, {"event": "x", "data": {}})
    assert status == "failed: HTTP 503"
    assert len(delivered) == 4, "one attempt + three retries on server errors"

    delivered.clear()
    state["status"] = 410
    status = await deliver_one(ep, {"event": "x", "data": {}})
    assert status == "failed: HTTP 410"
    assert len(delivered) == 1, "client errors are not retried"


async def test_purchase_flow_fires_webhooks(db, user, purchase, binance_key,
                                            capture_delivery, monkeypatch):
    delivered, _ = capture_delivery
    from api.services.purchase_service import PurchaseService
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    db.add(WebhookEndpoint(user_id=user.id, url="https://example.com/h", secret="s"))
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"
    events = [json.loads(r.content)["event"] for r in delivered]
    assert "purchase.completed" in events


async def test_integrations_listing(db, user, binance_key):
    async with client() as c:
        items = (await c.get("/api/integrations/available",
                             headers=auth_headers(user))).json()
    by = {i["name"]: i for i in items}
    assert by["binance"]["status"] == "connected"
    assert by["okx"]["status"] == "available"
    assert by["webhooks"]["kind"] == "automation"
    assert by["slack"]["kind"] == "alerting"
