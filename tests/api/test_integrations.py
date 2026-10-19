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

    def __init__(self, api_key, api_secret, passphrase="", exchange="binance", account_mode="unknown"):
        self.exchange = exchange
        self.passphrase = passphrase

    def spot_symbol(self, base, quote="USDT"):
        return f"{base}{quote}"

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


async def _instant_sleep(_):
    return None


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
    assert buy["order_id"].startswith("ORD-1-") and buy["tx_type"] == "buy" and buy["dry_run"] is True
    assert wd["withdrawal_id"].startswith("WD-1-") and wd["tx_type"] == "withdrawal" and wd["wallet_address"]


async def test_integrations_listing(db, user, binance_key):
    async with client() as c:
        items = (await c.get("/api/integrations/available",
                             headers=auth_headers(user))).json()
    by = {i["name"]: i for i in items}
    assert by["binance"]["status"] == "connected"
    assert by["bitget"]["status"] == "available"
    assert "okx" not in by and "bybit" not in by
    assert by["webhooks"]["kind"] == "automation"
    assert by["slack"]["kind"] == "alerting"
