"""v0.8.0: Unified Trading Account (Bitget v3) support alongside Classic."""
import inspect

import pytest

from api.services import exchange_gateway, key_verification as kv, telegram, unified_check as uc
from api.services.exchange_client import ExchangeClient, ExchangeClientError, interpret_unified_permissions
from bitget_client import BitgetClient, BitgetUnifiedClient
from tests.conftest_autobuy import make_key, make_purchase

pytestmark = pytest.mark.asyncio
UNIFIED = ExchangeClientError("Bitget error 40085: You are in Unified Account mode, and the Classic Account API is not supported at this time", "40085")
CLASSIC = ExchangeClientError("Bitget error 40084: You are in Classic Account mode, and the Unified Account API is not supported at this time", "40084")


class Recorder:
    """Replaces BitgetClient._request: records (method, path, params, body) and answers from a script."""
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, method, path, params=None, body=None, auth=True):   # bound as a class attribute: no self from the client
        self.calls.append((method, path, params, body))
        a = self.answers.get(path)
        if isinstance(a, Exception):
            raise a
        return a


# ---------------------------------------------------------------- the Classic client is untouched

def test_classic_client_source_has_no_v3_and_classic_requests_are_unchanged(monkeypatch):
    assert "/api/v3/" not in inspect.getsource(BitgetClient)
    rec = Recorder({"/api/v2/spot/account/info": {"userId": "1", "authorities": ["stow"]},
                    "/api/v2/spot/account/assets": [{"coin": "USDT", "available": "12.5"}],
                    "/api/v2/spot/trade/place-order": {"orderId": "o1", "clientOid": "ab-1"},
                    "/api/v2/spot/trade/orderInfo": [{"status": "filled", "orderId": "o1", "baseVolume": "1", "quoteVolume": "10", "priceAvg": "10", "feeDetail": None}],
                    "/api/v2/spot/trade/fills": []})
    monkeypatch.setattr(BitgetClient, "_request", rec)
    c = BitgetClient("k", "s", "p")
    c.get_account_info(); c.get_spot_available("USDT"); c.place_market_buy("XRPUSDT", 10.0, client_oid="ab-1")
    c.get_order_info("XRPUSDT", order_id="o1"); c.get_fills("XRPUSDT", "o1")
    assert [(m, p) for m, p, *_ in rec.calls] == [
        ("GET", "/api/v2/spot/account/info"), ("GET", "/api/v2/spot/account/assets"), ("POST", "/api/v2/spot/trade/place-order"),
        ("GET", "/api/v2/spot/trade/orderInfo"), ("GET", "/api/v2/spot/trade/fills")]
    assert rec.calls[2][3] == {"symbol": "XRPUSDT", "side": "buy", "orderType": "market", "force": "gtc", "size": "10.0", "clientOid": "ab-1"}


def test_wrapper_picks_the_client_by_mode():
    assert type(ExchangeClient("k", "s", "p", exchange="bitget")._client) is BitgetClient            # default: unknown -> Classic
    assert type(ExchangeClient("k", "s", "p", exchange="bitget", account_mode="classic")._client) is BitgetClient
    assert type(ExchangeClient("k", "s", "p", exchange="bitget", account_mode="unknown")._client) is BitgetClient
    assert type(ExchangeClient("k", "s", "p", exchange="bitget", account_mode="unified")._client) is BitgetUnifiedClient
    assert type(ExchangeClient("k", "s", "p", exchange="bitget", account_mode="garbage")._client) is BitgetClient


# ---------------------------------------------------------------- the Unified client

def test_unified_client_calls_v3_and_keeps_the_classic_shapes(monkeypatch):
    rec = Recorder({
        "/api/v3/account/info": {"userId": "7", "permType": "read-and-write", "permissions": ["uta_trade", "uta_mgt"], "ips": ""},
        "/api/v3/account/assets": {"accountEquity": "999", "assets": [{"coin": "USDT", "available": "40.5", "locked": "1", "balance": "41.5", "equity": "41.5", "debt": "0.5"},
                                                                       {"coin": "BTC", "available": "0.01", "debt": "0"}]},
        "/api/v3/trade/place-order": {"orderId": "u1", "clientOid": "ab-2"},
        "/api/v3/trade/order-details": [{"orderId": "u1", "clientOid": "ab-2", "orderStatus": "filled", "cumExecQty": "2.5", "cumExecValue": "10",
                                         "avgPrice": "4", "feeDetail": [{"feeCoin": "XRP", "fee": "-0.0025"}]}],
        "/api/v3/trade/fills": {"list": [{"execId": "f1", "orderId": "u1", "execPrice": "4", "execQty": "2.5", "execValue": "10", "feeDetail": [{"feeCoin": "XRP", "fee": "-0.0025"}]}]},
    })
    monkeypatch.setattr(BitgetClient, "_request", rec)
    c = BitgetUnifiedClient("k", "s", "p")
    assert c.get_account_info()["authorities"] == ["uta_trade", "uta_mgt"] and c.get_account_info()["perm_type"] == "read-and-write"
    assert c.get_spot_available("USDT") == 40.0          # available minus debt, never equity or totals
    assert c.get_spot_available("ETH") == 0.0
    placed = c.place_market_buy("XRPUSDT", 10.0, client_oid="ab-2")
    assert placed["orderId"] == "u1" and placed["executedQty"] == "2.5" and placed["cummulativeQuoteQty"] == "10"
    info = c.get_order_info("XRPUSDT", order_id="u1")
    assert info == {"status": "filled", "orderId": "u1", "clientOid": "ab-2", "baseVolume": "2.5", "quoteVolume": "10", "priceAvg": "4",
                    "fee": 0.0025, "feeCoin": "XRP", "feeDetail": [{"feeCoin": "XRP", "fee": "-0.0025"}]}
    fills = c.get_fills("XRPUSDT", "u1")
    assert fills == [{"tradeId": "f1", "priceAvg": "4", "size": "2.5", "amount": "10", "feeDetail": [{"feeCoin": "XRP", "fee": "-0.0025"}]}]
    body = next(b for m, p, _, b in rec.calls if p == "/api/v3/trade/place-order")
    assert body == {"category": "SPOT", "symbol": "XRPUSDT", "side": "buy", "orderType": "market", "qty": "10.0", "clientOid": "ab-2"}
    assert all(p.startswith("/api/v3/") for _, p, *_ in rec.calls)
    with pytest.raises(Exception):
        c.withdraw("XRP", "r...", 1.0)


def test_unified_permissions_interpretation():
    f = interpret_unified_permissions
    assert f(["uta_trade", "uta_mgt"], "read-and-write")["can_trade"] is True and f(["uta_trade", "uta_mgt"], "read-and-write")["management_read"] is True
    assert f(["uta_trade"], "read-and-write")["management_read"] is False
    assert f(["uta_trade"], "read-only") == {"can_trade": False, "can_withdraw": False, "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": True, "management_read": False}
    assert f(["uta_trade", "withdraw"], "read-and-write")["can_withdraw"] is True
    assert f([], "")["permissions_known"] is False


# ---------------------------------------------------------------- verification detects and stores the mode

class Venue:
    """Factory stand-in for ExchangeClient: scripted per (api key, mode)."""
    script: dict = {}
    built: list = []

    def __init__(self, api_key, api_secret, passphrase="", exchange="bitget", account_mode="unknown"):
        self.api_key, self.account_mode = api_key, account_mode
        Venue.built.append((api_key, account_mode))

    def get_account_info(self):
        a = Venue.script.get((self.api_key, self.account_mode), Venue.script.get(self.account_mode))
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture(autouse=True)
def venue(monkeypatch):
    Venue.script, Venue.built = {}, []
    monkeypatch.setattr(kv, "ExchangeClient", Venue)
    monkeypatch.setattr(exchange_gateway, "ExchangeClient", Venue, raising=False)
    yield


OK_V2 = {"can_trade": True, "can_withdraw": False, "permissions": ["stow"], "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": False}
OK_V3 = {"can_trade": True, "can_withdraw": False, "permissions": ["uta_trade", "uta_mgt"], "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": False, "management_read": True}
TRADE_ONLY_V3 = {**OK_V3, "permissions": ["uta_trade"], "management_read": False}


async def test_verification_follows_40085_to_v3_and_stores_unified(db, user):
    key = await make_key(db, user, exchange="bitget")
    key.verified, key.verification_status, key.account_mode = False, "pending", "unknown"
    await db.commit()
    Venue.script = {"unknown": UNIFIED, "unified": OK_V3}
    out = await kv.verify_api_key(db, key)
    assert out.verified is True and out.result == "ok"
    await db.refresh(key)
    assert key.account_mode == "unified" and key.verified is True
    assert [m for _, m in Venue.built] == ["unknown", "unified"]


async def test_verification_on_v2_success_stores_classic_and_a_unified_key_switching_back_is_followed(db, user):
    key = await make_key(db, user, exchange="bitget")
    key.account_mode = "unknown"; await db.commit()
    Venue.script = {"unknown": OK_V2}
    assert (await kv.verify_api_key(db, key)).verified is True
    await db.refresh(key); assert key.account_mode == "classic"
    key.account_mode = "unified"; await db.commit()
    Venue.script = {"unified": CLASSIC, "classic": OK_V2}
    assert (await kv.verify_api_key(db, key)).verified is True
    await db.refresh(key); assert key.account_mode == "classic"


async def test_unified_key_without_management_read_is_refused_with_the_almost_there_message(db, user):
    """The two live Unified keys carry uta_trade only: they can order but cannot read a balance."""
    key = await make_key(db, user, exchange="bitget"); key.account_mode = "unified"; await db.commit()
    Venue.script = {"unified": TRADE_ONLY_V3}
    out = await kv.verify_api_key(db, key)
    assert out.verified is False and out.result == "unified_management_missing"
    assert out.detail == ("Almost there! In Bitget, edit this key and also tick Unified Trading Account → Management → Read, "
                          "then verify again. AutoBuy needs it to check your balance before each buy.")
    await db.refresh(key)
    assert key.verified is False and key.verification_status == "failed" and key.verification_error == out.detail and key.account_mode == "unified"


async def test_unified_key_with_trade_and_management_read_verifies(db, user):
    key = await make_key(db, user, exchange="bitget"); key.account_mode = "unified"; await db.commit()
    Venue.script = {"unified": OK_V3}
    out = await kv.verify_api_key(db, key)
    assert out.verified is True and out.result == "ok"


async def test_verification_read_only_unified_key_is_refused(db, user):
    key = await make_key(db, user, exchange="bitget"); key.account_mode = "unified"; await db.commit()
    Venue.script = {"unified": {**OK_V3, "can_trade": False, "read_only": True}}
    out = await kv.verify_api_key(db, key)
    assert out.verified is False and out.result == "read_only_key"


# ---------------------------------------------------------------- the daily check switches instead of flagging

@pytest.fixture
def mail(monkeypatch):
    sent = []
    class Capture:
        async def queue(self, to, subject, html, text, template="generic", category="confirmation", user_id=None):
            sent.append({"to": to, "subject": subject, "text": text, "template": template}); return True
    monkeypatch.setattr(uc, "email_service", Capture()); return sent


@pytest.fixture
def tg(monkeypatch):
    sent = []
    async def fake_send(text, key=None):
        sent.append((text, key)); return True
    monkeypatch.setattr(telegram, "send", fake_send); return sent


async def test_daily_check_switches_a_key_that_turned_unified_and_pauses_nothing(db, user, mail, tg):
    from api.routes.exchange import encrypt_value
    key = await make_key(db, user, exchange="bitget")
    key.encrypted_api_key = encrypt_value("turned-unified-key"); key.verified, key.verification_status, key.account_mode = True, "verified", "classic"
    p = await make_purchase(db, user, key, crypto_symbol="XRP", status="active")
    await db.commit()
    Venue.script = {("turned-unified-key", "classic"): UNIFIED, ("turned-unified-key", "unified"): OK_V3, "classic": OK_V2, "unknown": OK_V2}
    out = await uc.check_unified_accounts(db)
    assert out["switched"] == 1 and out["turned_unified"] == 0 and out["paused"] == 0 and out["emails"] == 0
    await db.refresh(key); await db.refresh(p)
    assert key.account_mode == "unified" and key.verified is True and p.status == "active"
    assert mail == [] and any(k == f"unified_switched:{key.id}" for _, k in tg)


async def test_daily_check_still_flags_when_v3_also_refuses(db, user, mail, tg):
    from api.routes.exchange import encrypt_value
    key = await make_key(db, user, exchange="bitget")
    key.encrypted_api_key = encrypt_value("broken-unified-key"); key.verified, key.verification_status, key.account_mode = True, "verified", "classic"
    p = await make_purchase(db, user, key, crypto_symbol="XRP", status="active")
    await db.commit()
    Venue.script = {("broken-unified-key", "classic"): UNIFIED, ("broken-unified-key", "unified"): UNIFIED, "classic": OK_V2, "unknown": OK_V2}
    out = await uc.check_unified_accounts(db)
    assert out["turned_unified"] == 1 and out["paused"] == 1 and out["emails"] == 1 and out["switched"] == 0
    await db.refresh(key); await db.refresh(p)
    assert key.verified is False and p.status == "paused"


# ---------------------------------------------------------------- re-verify after the deploy

async def test_reverify_flagged_keys_verifies_through_v3_stores_the_mode_emails_and_resumes_nothing(db, user, mail, tg):
    from api.routes.exchange import encrypt_value
    key = await make_key(db, user, exchange="bitget")
    key.encrypted_api_key = encrypt_value("flagged-key"); key.verified, key.verification_status = False, "failed"
    key.verification_error = kv.unified_message(); key.account_mode = "unknown"
    p = await make_purchase(db, user, key, crypto_symbol="XRP", status="paused")
    await db.commit()
    Venue.script = {("flagged-key", "unknown"): UNIFIED, ("flagged-key", "unified"): OK_V3}
    out = await uc.reverify_flagged(db)
    assert out["flagged"] >= 1 and out["verified"] >= 1 and out["emails"] >= 1
    await db.refresh(key); await db.refresh(p)
    assert key.verified is True and key.account_mode == "unified" and key.verification_error is None
    assert p.status == "paused"   # nothing is resumed: the customer presses Resume
    ours = [m for m in mail if m["to"] == user.email]
    assert len(ours) == 1 and "Good news: AutoBuy now works with your Bitget account. Press Resume on your purchases whenever you're ready." in ours[0]["text"]


# ---------------------------------------------------------------- the executor switches on 40085 only, same client order id

class SwitchingGateway:
    """Refuses with 40085 while the key is Classic/unknown, fills once it is unified; records every place_buy."""
    def __init__(self, error_code="40085"):
        self.calls, self.error_code = [], error_code

    def place_buy(self, account, purchase, client_oid, dry_run):
        self.calls.append((account.account_mode, client_oid))
        if account.account_mode != "unified":
            raise exchange_gateway.ExecutionError(f"{self.error_code}: refused", stage="buy", code=self.error_code)
        return {"order_id": "u-1", "dry_run": dry_run, "reference_price": 4.0}

    def fetch_fill(self, account, purchase, order_id, client_oid, dry_run):
        return {"order_id": order_id, "filled": True, "raw_status": "filled", "amount_crypto": purchase.amount_usd / 4.0,
                "exchange_rate": 4.0, "cost_usdt": purchase.amount_usd, "fee": None, "fee_currency": None}

    def lookup_order(self, account, purchase, client_oid):
        return {"state": "not_found", "fill": None}


async def test_executor_switches_the_key_on_40085_and_retries_the_same_attempt(db, user):
    from api.services import purchase_executor as pe
    key = await make_key(db, user, exchange="bitget"); key.account_mode = "classic"; await db.commit()
    p = await make_purchase(db, user, key, crypto_symbol="XRP", status="active", amount_usd=10.0)
    await db.refresh(p, ["user", "exchange_account", "transactions"])
    gw = SwitchingGateway()
    tx = await pe.execute_one(db, p, pe.utcnow(), gw, dry_run=False)
    assert tx is not None and tx.status == "success" and tx.order_id == "u-1"
    assert len(gw.calls) == 2 and gw.calls[0][0] == "classic" and gw.calls[1][0] == "unified" and gw.calls[0][1] == gw.calls[1][1] == tx.client_oid
    await db.refresh(key); assert key.account_mode == "unified"


async def test_executor_never_switches_on_another_error(db, user):
    from api.services import purchase_executor as pe
    key = await make_key(db, user, exchange="bitget"); key.account_mode = "classic"; await db.commit()
    p = await make_purchase(db, user, key, crypto_symbol="XRP", status="active", amount_usd=10.0)
    await db.refresh(p, ["user", "exchange_account", "transactions"])
    gw = SwitchingGateway(error_code="40018")
    tx = await pe.execute_one(db, p, pe.utcnow(), gw, dry_run=False)
    assert tx.status == "failed" and len(gw.calls) == 1
    await db.refresh(key); assert key.account_mode == "classic"


# ---------------------------------------------------------------- withdraw mode is refused on a Unified key

async def test_withdraw_mode_purchase_is_refused_on_a_unified_key(db, user, client, monkeypatch):
    import httpx
    from api.auth import create_access_token
    from api.config import settings
    monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", user.email)
    key = await make_key(db, user, exchange="bitget"); key.verified, key.account_mode = True, "unified"; await db.commit()
    H = {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}
    body = {"crypto_symbol": "BTC", "amount_usd": 60, "frequency": "weekly", "purchase_type": "autobuy", "exchange_account_id": str(key.id),
            "wallet_address": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", "blockchain_network": "BTC"}
    r = await client.post("/api/v2/purchases", json=body, headers=H)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "unified_account_withdraw"
    r2 = await client.post("/api/v2/purchases", json={**body, "purchase_type": "dca", "wallet_address": None, "blockchain_network": None}, headers=H)
    assert r2.status_code == 201, r2.text
