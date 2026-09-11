import httpx
import pytest

import api.routes.exchange as ex_mod
from api.auth import create_access_token
from api.services.binance_client import BinanceClientError


class GoodAccount:
    def __init__(self, *a, **k): pass
    def get_account_info(self):
        return {"can_trade": True, "can_withdraw": False, "permissions": ["SPOT"]}


class WithdrawyAccount(GoodAccount):
    def get_account_info(self):
        return {"can_trade": True, "can_withdraw": True, "permissions": ["SPOT"]}


class NoTradeAccount(GoodAccount):
    def get_account_info(self):
        return {"can_trade": False, "can_withdraw": False, "permissions": []}


class RejectedKey(GoodAccount):
    def get_account_info(self):
        raise BinanceClientError("Invalid API-key, IP, or permissions for action", "-2015")


@pytest.fixture
def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}


async def verify(client, user, key):
    async with client as c:
        return await c.post(f"/api/exchange/keys/{key.id}/verify", headers=auth(user))


async def test_verify_ok(db, user, binance_key, monkeypatch, client):
    from api.services import binance_client as bc_mod
    monkeypatch.setattr(bc_mod, "BinanceClient", GoodAccount)
    monkeypatch.setattr("api.services.binance_client.BinanceClient", GoodAccount)
    import api.routes.exchange  # route imports lazily inside the handler
    monkeypatch.setattr("api.routes.exchange.BinanceClient", GoodAccount, raising=False)
    r = await verify(client, user, binance_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["result"] == "ok"
    assert body["withdrawal_permission_warning"] is False
    await db.refresh(binance_key)
    assert binance_key.verified is True


async def test_verify_warns_on_withdrawal_permission(db, user, binance_key, monkeypatch, client):
    monkeypatch.setattr("api.services.binance_client.BinanceClient", WithdrawyAccount)
    r = await verify(client, user, binance_key)
    assert r.json()["withdrawal_permission_warning"] is True


async def test_verify_no_trade_permission(db, user, binance_key, monkeypatch, client):
    monkeypatch.setattr("api.services.binance_client.BinanceClient", NoTradeAccount)
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "no_trade_permission"
    await db.refresh(binance_key)
    assert binance_key.verified is False


async def test_verify_invalid_key(db, user, binance_key, monkeypatch, client):
    monkeypatch.setattr("api.services.binance_client.BinanceClient", RejectedKey)
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "invalid_key"
    assert "allowlist" in body["detail"]


async def test_executor_refuses_unverified_key(db, user, binance_key):
    """The Auto-Buy executor never trades on a key the user has not verified."""
    from datetime import timedelta
    from sqlalchemy import select
    from api.models import PurchaseTransaction, utcnow
    from api.tasks import purchase_scheduler as ps
    from tests.api.conftest import make_autobuy_purchase
    from tests.test_purchase_events import RichGateway
    binance_key.verified = False
    await db.commit()
    p = await make_autobuy_purchase(db, user, key=binance_key, next_execution=utcnow() - timedelta(minutes=1))
    gw = RichGateway()
    counts = await ps.execute_due_purchases(db, gateway=gw, dry_run=True)
    assert counts["failed"] == 1 and not hasattr(gw, "calls")
    tx = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalar_one()
    assert tx.status == "failed" and "not verified" in tx.error_message
