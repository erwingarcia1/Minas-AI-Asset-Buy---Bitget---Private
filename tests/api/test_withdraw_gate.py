"""Private-beta withdraw gate: every path into withdraw mode is refused for a
non-allowlisted owner, allowed for an allowlisted one, judged by the purchase
OWNER (never the acting admin); accumulate purchases are untouched; the API
default is accumulate; admins can resolve a parked withdraw-mode purchase by
keeping the coins on the exchange."""
import uuid

import httpx
import pytest
from sqlalchemy import select

from api.config import settings
from api.models import Purchase, PurchaseTransaction, User
from api.services import withdraw_gate
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import bitget_key, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW
from tests.test_withdrawal_ops import reload, sent_withdrawal

BTC_ADDR = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
MSG = "Sending coins to your own wallet isn't available during the private beta. Your coins stay in your Bitget sub-account."


def api():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def auth(u):
    from api.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(str(u.id)).access_token}"}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "withdrawals_enabled", "0")
    monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", "")


@pytest.fixture
async def admin(db):
    u = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(u); await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh); await db.commit()


def create_body(key, **kw):
    return {"crypto_symbol": "BTC", "amount_usd": 50, "frequency": "weekly", "exchange_account_id": str(key.id), **kw}


def test_gate_helper_reads_the_global_switch_and_the_owner_allowlist(monkeypatch):
    class U:  # noqa: D401
        def __init__(self, email): self.email = email
    monkeypatch.setattr(settings, "withdrawals_enabled", "0")
    monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", " Support@minastech.ca , other@x.io")
    assert withdraw_gate.withdrawals_allowed(U("support@minastech.ca")) is True
    assert withdraw_gate.withdrawals_allowed(U("SUPPORT@MINASTECH.CA")) is True
    assert withdraw_gate.withdrawals_allowed(U("someone@example.com")) is False
    assert withdraw_gate.withdrawals_allowed(None) is False
    monkeypatch.setattr(settings, "withdrawals_enabled", "1")
    assert withdraw_gate.withdrawals_allowed(U("someone@example.com")) is True


async def test_create_in_withdraw_mode_is_refused_and_accumulate_is_the_default(db, user, bitget_key, gated):
    async with api() as c:
        r = await c.post("/api/v2/purchases", headers=auth(user), json=create_body(bitget_key, purchase_type="autobuy", wallet_address=BTC_ADDR, blockchain_network="BTC"))
        assert r.status_code == 403 and r.json()["detail"] == {"error": "withdrawals_disabled", "details": {"message": MSG}}
        r = await c.post("/api/v2/purchases", headers=auth(user), json=create_body(bitget_key, withdraw_immediately=True, wallet_address=BTC_ADDR, blockchain_network="BTC"))
        assert r.status_code == 403, "the deprecated alias is gated too"
        r = await c.post("/api/v2/purchases", headers=auth(user), json=create_body(bitget_key))
        assert r.status_code == 201
        pid = r.json()["purchase_id"]
        got = (await c.get(f"/api/v2/purchases/{pid}", headers=auth(user))).json()
        assert got["purchase_type"] == "dca" and got["withdraw_immediately"] is False, "omitted purchase_type = accumulate"
        # editing an accumulate purchase is unaffected
        r = await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"amount_usd": 60})
        assert r.status_code == 200 and r.json()["amount_usd"] == 60
        # switching it to withdraw mode is refused
        r = await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"purchase_type": "autobuy", "wallet_address": BTC_ADDR, "confirm_wallet_address": BTC_ADDR, "blockchain_network": "BTC"})
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == MSG
        assert (await c.get(f"/api/v2/purchases/{pid}", headers=auth(user))).json()["purchase_type"] == "dca"
        # the customer profile says so, for the UI
        assert (await c.get("/api/user/profile", headers=auth(user))).json()["withdrawals_allowed"] is False


async def test_allowlisted_owner_can_create_and_switch(db, user, bitget_key, gated, monkeypatch):
    monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", user.email)
    async with api() as c:
        assert (await c.get("/api/user/profile", headers=auth(user))).json()["withdrawals_allowed"] is True
        r = await c.post("/api/v2/purchases", headers=auth(user), json=create_body(bitget_key, purchase_type="autobuy", wallet_address=BTC_ADDR, blockchain_network="BTC"))
        assert r.status_code == 201
        assert (await c.get(f"/api/v2/purchases/{r.json()['purchase_id']}", headers=auth(user))).json()["purchase_type"] == "autobuy"
        r = await c.post("/api/v2/purchases", headers=auth(user), json=create_body(bitget_key))
        pid = r.json()["purchase_id"]
        r = await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"purchase_type": "autobuy", "wallet_address": BTC_ADDR, "confirm_wallet_address": BTC_ADDR, "blockchain_network": "BTC"})
        assert r.status_code == 200 and r.json()["purchase_type"] == "autobuy"


async def test_retry_withdrawal_is_refused_for_a_non_allowlisted_owner(db, user, bitget_key, gated):
    p, buy, wd = await sent_withdrawal(db, user, bitget_key)
    p.status = "withdrawal_failed"; await db.commit()
    async with api() as c:
        r = await c.post(f"/api/v2/purchases/{p.id}/retry-withdrawal", headers=auth(user))
    assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == MSG
    assert (await reload(db, p.id)).status == "withdrawal_failed", "nothing changed"


async def test_admin_force_retry_is_judged_by_the_owner_not_the_admin(db, user, bitget_key, admin, gated, monkeypatch):
    monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", admin.email)   # the ADMIN is allowlisted; the owner is not
    p = await make_purchase(db, user, bitget_key, purchase_type="autobuy", status="failed", next_execution=None, failure_count=3)
    q = await make_purchase(db, user, bitget_key, purchase_type="dca", status="failed", next_execution=None, failure_count=3)
    async with api() as c:
        r = await c.post(f"/api/admin/purchases/{p.id}/force-retry", headers=auth(admin))
        assert r.status_code == 403 and "owner isn't allowed withdrawals" in r.json()["detail"]["details"]["message"]
        assert (await reload(db, p.id)).status == "failed"
        r = await c.post(f"/api/admin/purchases/{q.id}/force-retry", headers=auth(admin))
        assert r.status_code == 204, "an accumulate purchase re-arms as before"
        monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", f"{admin.email},{user.email}")
        r = await c.post(f"/api/admin/purchases/{p.id}/force-retry", headers=auth(admin))
        assert r.status_code == 204, "allowed once the OWNER is allowlisted"


async def park(db, user, key, purchase_type):
    p = await make_purchase(db, user, key, purchase_type=purchase_type, amount_usd=50, next_execution=NOW, wallet_address=BTC_ADDR, blockchain_network="BTC")
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(fill_factor=0.0), dry_run=False)
    p = await reload(db, p.id)
    assert p.status == "needs_review"
    return p


async def test_admin_resolve_keeps_coins_on_the_exchange_for_a_non_allowlisted_owner(db, user, bitget_key, admin, gated, monkeypatch):
    from api.routes.admin import purchases as admin_routes
    p = await park(db, user, bitget_key, "autobuy")
    gw = FakeGateway(lookup_state="filled")
    monkeypatch.setattr(admin_routes, "_gateway", lambda: gw)
    H = auth(admin)
    async with api() as c:
        await c.get(f"/api/admin/purchases/{p.id}/review", headers=H)
        r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H, json={"action": "record_fill", "note": "order filled 0.001 BTC on Bitget"})
        assert r.status_code == 403 and "keeping the coins on the exchange" in r.json()["detail"]["details"]["message"]
        assert (await reload(db, p.id)).status == "needs_review", "refused = nothing changed; the queue is not stuck"
        r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H,
                         json={"action": "record_fill", "keep_on_exchange": True, "note": "order filled 0.001 BTC on Bitget"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active" and r.json()["purchase_type"] == "dca"
    rows = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id).execution_options(populate_existing=True))).scalars().all()
    buy = next(t for t in rows if t.tx_type == "buy")
    assert buy.status == "success" and "coins kept on the exchange, purchase switched to accumulate" in buy.error_message
    assert not any(t.tx_type == "withdrawal" for t in rows), "no withdrawal row was created"
    assert gw.withdrawals == [] if hasattr(gw, "withdrawals") else True
    p = await reload(db, p.id)
    assert p.purchase_type == "dca" and p.status == "active"


async def test_existing_accumulate_purchases_are_unaffected_by_the_gate(db, user, bitget_key, gated):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=20, next_execution=NOW)
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    assert counts["executed"] == 1
    async with api() as c:
        r = await c.put(f"/api/v2/purchases/{p.id}", headers=auth(user), json={"status": "paused"})
        assert r.status_code == 200 and r.json()["status"] == "paused"
        r = await c.put(f"/api/v2/purchases/{p.id}", headers=auth(user), json={"status": "active"})
        assert r.status_code == 200 and r.json()["status"] == "active"
