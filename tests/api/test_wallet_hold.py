"""24-hour hold on new withdrawal destinations after a password reset or change."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from api.auth import hash_password
from api.integrations import resend
from api.models import User
from api.services import wallet_hold
from tests.api.conftest import auth_headers
from tests.conftest_autobuy import BTC_MAIN, make_key, make_purchase

OLD, NEW = "Old-Passw0rd!", "New-Passw0rd!"
OTHER_BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
P = "/api/v2/purchases"


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def account(db):
    u = User(email=f"hold-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password(OLD), kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


@pytest.fixture
def sent(monkeypatch):
    """Security emails are queued to the outbox (audit C2); capture at the outbox."""
    box = []

    async def fake_enqueue(to, subject, html, text, template="generic", *, category="confirmation", user_id=None, now=None):
        box.append({"to": to, "subject": subject, "text": text, "html": html, "template": template, "category": category})
        return True
    from api.services import email_outbox
    monkeypatch.setattr(email_outbox, "enqueue", fake_enqueue)
    return box


def body(key, **over):
    b = {"crypto_symbol": "BTC", "amount_usd": 60, "wallet_address": BTC_MAIN, "blockchain_network": "BTC",
         "frequency": "daily", "exchange_account_id": str(key.id), "purchase_type": "autobuy"}
    b.update(over)
    return b


# --------------------------------------------------------------------------- starting the hold

async def test_change_password_starts_the_hold_logs_it_and_emails_the_end_time(db, account, sent, caplog):
    import logging
    async with client() as c:
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": OLD})).json()
        H = {"Authorization": f"Bearer {login['access_token']}"}
        with caplog.at_level(logging.WARNING, logger="autobuy.wallet_hold"):
            r = await c.post("/api/auth/change-password", json={"current_password": OLD, "new_password": NEW}, headers=H)
        assert r.status_code == 204
        await asyncio.sleep(1.1)   # tokens carry whole-second iat: sessions from the change's own second are signed out
        login = (await c.post("/api/auth/login", json={"email": account.email, "password": NEW})).json()
        profile = (await c.get("/api/user/profile", headers={"Authorization": f"Bearer {login['access_token']}"})).json()
    await db.refresh(account)
    until = account.wallet_hold_until
    assert until is not None and account.wallet_hold_reason == "password_change"
    assert timedelta(hours=23, minutes=59) < until - account.password_changed_at <= timedelta(hours=24)
    assert profile["wallet_hold_until"] is not None and profile["wallet_hold_reason"] == "password_change", "the user can see when it ends"
    assert any("wallet change hold started" in r.message and "password_change" in r.message for r in caplog.records)
    mail = [m for m in sent if "password was changed" in m["subject"]]
    assert mail and "new withdrawal addresses are on hold until" in mail[0]["text"]
    assert until.strftime("%Y-%m-%d %H:%M UTC") in mail[0]["text"]


async def test_password_reset_starts_the_hold(db, account, sent):
    from tests.api.test_password_reset import link_token
    async with client() as c:
        await c.post("/api/auth/forgot-password", json={"email": account.email})
        token = link_token(sent[0])
        assert (await c.post("/api/auth/reset-password", json={"token": token, "new_password": NEW})).status_code == 204
    await db.refresh(account)
    assert account.wallet_hold_reason == "password_reset"
    assert wallet_hold.active_hold(account) is not None
    assert "on hold until" in [m for m in sent if "password was changed" in m["subject"]][0]["text"]


# --------------------------------------------------------------------------- what the hold blocks

@pytest.fixture
async def held(db, account):
    """A KYC-verified user under a fresh hold, with a Bitget key and one autobuy purchase to BTC_MAIN."""
    key = await make_key(db, account, exchange="bitget")
    existing = await make_purchase(db, account, key, purchase_type="autobuy", amount_usd=60, wallet_address=BTC_MAIN)
    wallet_hold.start_hold(account, "password_reset")
    await db.commit()
    return account, key, existing


async def test_blocked_actions_return_a_clear_error_with_the_end_time(db, held):
    account, key, existing = held
    H = auth_headers(account)
    until = account.wallet_hold_until.astimezone(timezone.utc).isoformat()
    async with client() as c:
        # 1. a new autobuy purchase to an address never used by this user
        r = await c.post(P, json=body(key, wallet_address=OTHER_BTC), headers=H)
        assert r.status_code == 422, r.text
        d = r.json()["detail"]
        assert d["error"] == "wallet_change_hold" and d["details"]["hold_until"] == until and d["details"]["reason"] == "password_reset"
        assert d["details"]["action"] == "create_purchase" and "after " in d["details"]["message"] and "24 hours" in d["details"]["message"]
        # 2. a new wallet kept on an accumulate purchase
        r = await c.post(P, json=body(key, wallet_address=OTHER_BTC, purchase_type="dca"), headers=H)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_change_hold"
        # 3. changing the address on an existing purchase
        r = await c.put(f"{P}/{existing.id}", json={"wallet_address": OTHER_BTC, "confirm_wallet_address": OTHER_BTC}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_change_hold"
        assert r.json()["detail"]["details"]["action"] == "change_wallet"
        # 4. switching an accumulate purchase to withdraw-immediately, even to the known address
        dca = await make_purchase(db, account, key, purchase_type="dca", wallet_address=None, blockchain_network=None)
        r = await c.put(f"{P}/{dca.id}", json={"purchase_type": "autobuy", "wallet_address": BTC_MAIN,
                                                "blockchain_network": "BTC", "confirm_wallet_address": BTC_MAIN}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["details"]["action"] == "switch_to_autobuy"
        # 5. saving a new address-book wallet
        r = await c.post("/api/wallets", json={"address": OTHER_BTC, "blockchain_network": "BTC", "label": "new"}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "wallet_change_hold"
    await db.refresh(existing)
    assert existing.wallet_address == BTC_MAIN and existing.status == "active", "nothing changed, the existing purchase keeps running"


async def test_addresses_already_used_keep_working_during_the_hold(db, held):
    account, key, existing = held
    H = auth_headers(account)
    async with client() as c:
        # a new autobuy purchase to the address already on a purchase
        r = await c.post(P, json=body(key, wallet_address=BTC_MAIN), headers=H)
        assert r.status_code == 201, r.text
        # case-insensitive match for EVM addresses saved in the address book
        eth = "0x52908400098527886E0F7030069857D2E4169EE7"
        from api.models.purchase import Wallet
        db.add(Wallet(user_id=account.id, address=eth, blockchain_network="ERC20"))
        await db.commit()
        r = await c.post(P, json=body(key, crypto_symbol="ETH", blockchain_network="ERC20", wallet_address=eth.lower(),
                                      purchase_type="dca"), headers=H)
        assert r.status_code == 201, r.text
        # pausing/resuming and changing the amount are unaffected
        assert (await c.put(f"{P}/{existing.id}", json={"status": "paused"}, headers=H)).status_code == 200
        assert (await c.put(f"{P}/{existing.id}", json={"status": "active", "amount_usd": 70}, headers=H)).status_code == 200


async def test_the_existing_purchase_still_executes_and_can_retry_its_withdrawal_during_the_hold(db, held, monkeypatch):
    from api.routes import autobuy_purchases as routes
    from api.tasks import purchase_scheduler as ps
    from tests.fake_gateway import FailingGateway, FakeGateway
    from tests.test_purchase_scheduler import NOW
    account, key, existing = held
    existing.next_execution = NOW
    await db.commit()
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    assert counts["executed"] == 1
    gw = FakeGateway()
    monkeypatch.setattr(routes, "_gateway", lambda: gw)
    async with client() as c:
        r = await c.post(f"{P}/{existing.id}/retry-withdrawal", headers=auth_headers(account))
    assert r.status_code == 200 and len(gw.withdrawals) == 1, "the known address is never blocked"


async def test_the_hold_expires(db, held):
    account, key, existing = held
    account.wallet_hold_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    assert wallet_hold.active_hold(account) is None
    H = auth_headers(account)
    async with client() as c:
        assert (await c.post(P, json=body(key, wallet_address=OTHER_BTC), headers=H)).status_code == 201
        assert (await c.post("/api/wallets", json={"address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "blockchain_network": "BTC"},
                             headers=H)).status_code == 201
        profile = (await c.get("/api/user/profile", headers=H)).json()
    assert profile["wallet_hold_until"] is None and profile["wallet_hold_reason"] is None


def test_start_hold_never_shortens_an_existing_hold():
    u = User(email="x@example.com", password_hash="x")
    now = datetime.now(timezone.utc)
    first = wallet_hold.start_hold(u, "password_reset", now)
    second = wallet_hold.start_hold(u, "password_change", now - timedelta(hours=5))
    assert first == second == now + timedelta(hours=24) and u.wallet_hold_reason == "password_change"
