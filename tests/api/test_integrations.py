import json

import httpx
import pytest
from sqlalchemy import select

import api.services.webhook_service as wh_mod
from api.models import WebhookEndpoint, utcnow
from api.services.webhook_service import deliver_one, sign
from tests.api.conftest import auth_headers, make_autobuy_purchase


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

    def place_buy(self, symbol, usdt_amount, client_oid, dry_run=True):
        VenueRecorder.calls.append((self.exchange, symbol, self.passphrase))
        return {"order_id": "1", "dry_run": dry_run}

    def fetch_fill(self, symbol, usdt_amount, order_id, client_oid, dry_run=True):
        return {"order_id": order_id, "filled": True, "raw_status": "filled", "amount_crypto": usdt_amount / 100.0,
                "exchange_rate": 100.0, "cost_usdt": usdt_amount, "fee": None, "fee_currency": None}

    def lookup_order(self, symbol, client_oid):
        return {"state": "not_found", "fill": None}


async def test_executor_routes_by_purchase_exchange(db, user, monkeypatch):
    """The Auto-Buy gateway builds the client for the purchase's exchange account:
    OKX venue, OKX symbol format, decrypted passphrase."""
    from datetime import timedelta
    from api.routes.exchange import encrypt_value
    from api.models import APIKey
    from api.tasks import purchase_scheduler as ps
    monkeypatch.setattr(ps, "ExchangeClient", VenueRecorder, raising=False)
    import api.services.binance_client as bc
    monkeypatch.setattr(bc, "ExchangeClient", VenueRecorder)
    VenueRecorder.calls = []
    key = APIKey(user_id=user.id, exchange="okx",
                 encrypted_api_key=encrypt_value("okx-key-0000000000"),
                 encrypted_api_secret=encrypt_value("okx-sec-0000000000"),
                 encrypted_passphrase=encrypt_value("okx-passphrase"),
                 verified=True)
    db.add(key)
    await db.commit()
    await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100, key=key, purchase_type="dca",
                                next_execution=utcnow() - timedelta(minutes=1))
    counts = await ps.execute_due_purchases(db, gateway=ps.ExchangeClientGateway(), dry_run=True)
    assert counts["executed"] == 1
    assert VenueRecorder.calls == [("okx", "BTC-USDT", "okx-passphrase")]


async def test_okx_key_requires_passphrase(db, user, monkeypatch):
    from api.config import settings
    monkeypatch.setattr(settings, "supported_exchanges_csv", "bitget,okx")   # OKX is off by default
    async with client() as c:
        r = await c.post("/api/exchange/keys", headers=auth_headers(user),
                         json={"exchange": "okx", "api_key": "x" * 20,
                               "api_secret": "y" * 20})
        assert r.status_code == 422 and "passphrase" in r.text
        ok = await c.post("/api/exchange/keys", headers=auth_headers(user),
                          json={"exchange": "okx", "api_key": "x" * 20,
                                "api_secret": "y" * 20, "passphrase": "p" * 10})
        assert ok.status_code == 201


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


async def test_purchase_flow_fires_webhooks(db, user, binance_key, capture_delivery):
    delivered, _ = capture_delivery
    from datetime import timedelta
    from api.tasks import purchase_scheduler as ps
    from tests.test_purchase_events import RichGateway
    db.add(WebhookEndpoint(user_id=user.id, url="https://example.com/h", secret="s"))
    await db.commit()
    await make_autobuy_purchase(db, user, key=binance_key, next_execution=utcnow() - timedelta(minutes=1))
    counts = await ps.execute_due_purchases(db, gateway=RichGateway(), dry_run=True)
    assert counts["executed"] == 1
    events = [json.loads(r.content)["event"] for r in delivered]
    assert events == ["purchase.buy_executed", "purchase.withdrawal_sent"]
    buy, wd = (json.loads(r.content)["data"] for r in delivered)
    assert buy["order_id"] == "ORD-1" and buy["tx_type"] == "buy" and buy["dry_run"] is True
    assert wd["withdrawal_id"] == "WD-1" and wd["tx_type"] == "withdrawal" and wd["wallet_address"]


async def test_integrations_listing(db, user, binance_key):
    async with client() as c:
        items = (await c.get("/api/integrations/available",
                             headers=auth_headers(user))).json()
    by = {i["name"]: i for i in items}
    assert by["binance"]["status"] == "connected"
    assert by["okx"]["status"] == "available"
    assert by["webhooks"]["kind"] == "automation"
    assert by["slack"]["kind"] == "alerting"
