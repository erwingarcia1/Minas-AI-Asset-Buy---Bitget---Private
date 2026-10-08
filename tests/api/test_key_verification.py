"""Exchange key verification: every outcome (verified, bad credentials, IP
allowlist rejection, venue unreachable, no trade permission) is written to the
row with a readable reason and timestamps, and logged without credentials."""
import logging

import httpx
import pytest

from api.auth import create_access_token
from api.services import key_verification as kv
from api.services.exchange_client import ExchangeClientError

SECRETS = ("test-api-key-000001", "test-api-secret-01")


class Venue:
    """Fake ExchangeClient: records what it was built with, answers get_account_info."""
    built = []
    info = {"can_trade": True, "can_withdraw": False, "permissions": ["readonly", "trade"], "permissions_known": True}
    error = None

    def __init__(self, api_key, api_secret, passphrase="", exchange="binance"):
        Venue.built.append((exchange, api_key, api_secret, passphrase))
        self.exchange = exchange

    def get_account_info(self):
        if Venue.error is not None:
            raise Venue.error
        return dict(Venue.info)


@pytest.fixture(autouse=True)
def reset_venue(monkeypatch):
    Venue.built, Venue.error = [], None
    Venue.info = {"can_trade": True, "can_withdraw": False, "permissions": ["readonly", "trade"], "permissions_known": True}
    monkeypatch.setattr(kv, "ExchangeClient", Venue)
    yield


def fresh():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def client():
    return fresh()


def auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}


async def verify(_client, user, key):
    """One request per fresh client, so a test can verify several times."""
    async with fresh() as c:
        return await c.post(f"/api/exchange/keys/{key.id}/verify", headers=auth(user))


async def assert_row(db, key, *, verified, status, error_contains=None, withdrawal=None, before):
    await db.refresh(key)
    assert key.verified is verified and key.verification_status == status
    assert key.last_checked_at is not None and key.last_checked_at > before
    assert key.updated_at > before, "the row must be written on every verification"
    if verified:
        assert key.verified_at is not None and key.verification_error is None
    else:
        assert key.verification_error and (error_contains or "") in key.verification_error
    assert key.withdrawal_enabled is withdrawal


async def test_verified_writes_row_and_logs_info(db, user, binance_key, client, caplog):
    before = binance_key.updated_at
    with caplog.at_level(logging.INFO, logger="autobuy.exchange"):
        r = await verify(client, user, binance_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["result"] == "ok" and body["verification_status"] == "verified"
    assert body["withdrawal_permission_warning"] is False and body["key"]["verified"] is True
    assert body["verified_at"] and body["last_checked_at"]
    await assert_row(db, binance_key, verified=True, status="verified", withdrawal=False, before=before)
    assert Venue.built[0] == ("binance", SECRETS[0], SECRETS[1], ""), "decrypted credentials reach the client"
    rec = next(r for r in caplog.records if r.getMessage() == "api_key_verified")
    assert rec.exchange == "binance" and rec.withdrawal_enabled is False
    assert SECRETS[0] not in caplog.text and SECRETS[1] not in caplog.text


async def test_withdrawal_permission_is_recorded_and_warned(db, user, binance_key, client):
    Venue.info = {"can_trade": True, "can_withdraw": True, "permissions": ["trade", "withdraw"], "permissions_known": True}
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["withdrawal_permission_warning"] is True and body["withdrawal_enabled"] is True
    assert "can also withdraw" in body["detail"]
    await assert_row(db, binance_key, verified=True, status="verified", withdrawal=True, before=before)


async def test_bad_credentials(db, user, binance_key, client, caplog):
    Venue.error = ExchangeClientError("Bitget error 40012: apikey/password is incorrect", "40012")
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    with caplog.at_level(logging.WARNING, logger="autobuy.exchange"):
        body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "invalid_key" and body["verification_status"] == "failed"
    assert "40012" in body["detail"] and "passphrase" in body["detail"]
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="40012", before=before)
    rec = next(r for r in caplog.records if r.getMessage() == "api_key_verification_failed")
    assert rec.exchange_code == "40012" and rec.result == "invalid_key"
    assert SECRETS[0] not in caplog.text and SECRETS[1] not in caplog.text


async def test_ip_allowlist_rejection(db, user, binance_key, client):
    Venue.error = ExchangeClientError("Bitget error 40018: Invalid IP", "40018")
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["result"] == "ip_not_allowed" and body["verified"] is False
    assert "IP" in body["detail"] and "allowlist" in body["detail"]
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="IP", before=before)
    # Binance folds the IP case into -2015; the message decides
    Venue.error = ExchangeClientError("Binance error -2015: Invalid API-key, IP, or permissions for action", "-2015")
    binance_key.exchange = "binance"
    await db.commit()
    body = (await verify(client, user, binance_key)).json()
    assert body["result"] == "invalid_key" and "IP allowlist" in body["detail"]


async def test_venue_unreachable(db, user, binance_key, client):
    Venue.error = ExchangeClientError("Bitget error NETWORK: GET /api/v2/spot/account/info: ConnectionError: boom", "NETWORK")
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["result"] == "unreachable" and body["verified"] is False
    assert body["detail"].startswith("Bitget unreachable")
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="unreachable", before=before)


async def test_no_trade_permission(db, user, binance_key, client):
    Venue.info = {"can_trade": False, "can_withdraw": False, "permissions": ["readonly"], "permissions_known": True}
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "no_trade_permission"
    assert "readonly" in body["detail"]
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="spot trading", withdrawal=False, before=before)


async def test_read_only_bitget_key_is_refused_with_the_fix_it_message(db, user, binance_key, client):
    """Seen live on 2026-10-06: a Read-only Bitget key with Spot ticked reports 'stor', passed
    verification as 'verified' (unconfirmed code) and then every buy failed with 40014."""
    from api.services.exchange_client import interpret_bitget_authorities
    Venue.info = {**interpret_bitget_authorities(["stor"]), "permissions": ["stor"]}
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "read_only_key"
    assert body["detail"] == ("This key is Read-only, so AutoBuy can't place buys. In Bitget, edit the key to Read-write "
                              "with only Spot → Trade ticked, then verify again.")
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="Read-only", withdrawal=False, before=before)


async def test_read_write_bitget_key_with_spot_trade_verifies(db, user, binance_key, client):
    from api.services.exchange_client import interpret_bitget_authorities
    Venue.info = {**interpret_bitget_authorities(["stow"]), "permissions": ["stow"]}
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["result"] == "ok" and body["withdrawal_enabled"] is False
    await assert_row(db, binance_key, verified=True, status="verified", withdrawal=False, before=before)


async def test_unified_account_is_refused_with_the_fix_it_message(db, user, binance_key, client, caplog):
    """Bitget answers every Classic (v2) call with 40085 once the account is migrated to Unified Trading mode."""
    Venue.error = ExchangeClientError("Bitget error 40085: You are in Unified Account mode, and the Classic Account API is not supported at this time", "40085")
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "unified_account"
    assert body["detail"] == ("This Bitget account uses Unified Trading mode, which AutoBuy doesn't support yet. We're adding it now. "
                              "Please contact support@minastech.ca and we'll let you know as soon as it's ready.")
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="Unified Trading mode", withdrawal=None, before=before)


async def test_classic_account_with_spot_trade_still_verifies_after_the_unified_rule(db, user, binance_key, client):
    from api.services.exchange_client import interpret_bitget_authorities
    Venue.info = {**interpret_bitget_authorities(["stow"]), "permissions": ["stow"]}
    binance_key.exchange = "bitget"
    await db.commit()
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["result"] == "ok"


async def test_permissions_not_reported_still_verifies(db, user, binance_key, client):
    Venue.info = {"can_trade": None, "can_withdraw": None, "permissions": [], "permissions_known": False}
    binance_key.exchange = "bitget"   # a Bitget key whose authorities list is empty/unrecognised
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["withdrawal_enabled"] is None and "does not report" in body["detail"]
    assert body["withdrawal_permission_warning"] is True
    await assert_row(db, binance_key, verified=True, status="verified", withdrawal=None, before=before)


async def test_failed_then_retry_succeeds_and_clears_error(db, user, binance_key, client):
    Venue.error = ExchangeClientError("Bitget error 40037: Apikey does not exist", "40037")
    await verify(client, user, binance_key)
    await db.refresh(binance_key)
    assert binance_key.verification_status == "failed" and binance_key.verified_at is None
    Venue.error = None
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True
    await db.refresh(binance_key)
    assert binance_key.verification_status == "verified" and binance_key.verification_error is None
    assert binance_key.verified_at is not None


async def test_credential_change_resets_verification(db, user, binance_key, client, monkeypatch):
    from api.config import settings
    monkeypatch.setattr(settings, "supported_exchanges_csv", "bitget,binance")
    await verify(client, user, binance_key)
    async with client as c:
        r = await c.post("/api/exchange/keys", headers=auth(user),
                         json={"exchange": "binance", "api_key": "new-key-0000000000", "api_secret": "new-secret-000000"})
    assert r.status_code == 201
    assert r.json()["verified"] is False and r.json()["verification_status"] == "pending"
    assert r.json()["verification_error"] is None and r.json()["verified_at"] is None
    async with fresh() as c:
        listed = (await c.get("/api/exchange/keys", headers=auth(user))).json()
    assert listed[0]["verification_status"] == "pending"


async def test_verify_unknown_key_is_404(user, client):
    import uuid
    async with client as c:
        assert (await c.post(f"/api/exchange/keys/{uuid.uuid4()}/verify", headers=auth(user))).status_code == 404


def test_classify_covers_every_venue_family():
    assert kv.classify("bitget", ExchangeClientError("x", "40006"))[0] == "invalid_key"
    assert kv.classify("bitget", ExchangeClientError("x", "40009"))[0] == "invalid_key"
    assert kv.classify("bitget", ExchangeClientError("Invalid IP", "40018"))[0] == "ip_not_allowed"
    assert kv.classify("bitget", ExchangeClientError("x", "HTTP"))[0] == "unreachable"
    assert kv.classify("bitget", ExchangeClientError("request IP is not in the whitelist", "99999"))[0] == "ip_not_allowed"
    assert kv.classify("bitget", ExchangeClientError("something else", "40001"))[0] == "error"


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
    assert counts["failed"] == 1 and gw.n == 0
    tx = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalar_one()
    assert tx.status == "failed" and "not verified" in tx.error_message


def test_bitget_authorities_interpretation():
    from api.services.exchange_client import interpret_bitget_authorities as f
    # documented words
    assert f(["readonly", "trade"]) == {"can_trade": True, "can_withdraw": False, "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": False}
    assert f(["readonly"]) == {"can_trade": False, "can_withdraw": False, "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": False}
    assert f(["trade", "withdraw"]) == {"can_trade": True, "can_withdraw": True, "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": False}
    # nothing / only unknown codes: never a rejection, never "safe"
    assert f([]) == {"can_trade": None, "can_withdraw": None, "permissions_known": False, "unconfirmed": [], "extra": [], "read_only": False}
    assert f(["zzz"]) == {"can_trade": None, "can_withdraw": None, "permissions_known": False, "unconfirmed": ["zzz"], "extra": [], "read_only": False}
    # Bitget v2 codes seen live: stor = Spot -> Read (Read-only key), stow = Spot -> Trade (Read-write key)
    assert f(["stor"]) == {"can_trade": False, "can_withdraw": False, "permissions_known": True, "unconfirmed": [], "extra": [], "read_only": True}
    assert f(["stor", "stow"])["read_only"] is False and f(["stor", "stow"])["can_trade"] is True


@pytest.mark.parametrize("code, trade, extra", [
    ("stow", True, []),         # Spot -> Trade: the one AutoBuy needs
    ("smow", False, ["smow"]),  # Margin -> Trade
    ("taxw", False, ["taxw"]),  # Taxation -> Search
    ("p2pw", False, ["p2pw"]),  # P2P -> Search
    ("wtow", False, ["wtow"]),  # Wallet -> Transfer (internal, not an external withdrawal)
])
def test_each_confirmed_bitget_code_is_mapped_and_none_grants_withdrawal(code, trade, extra):
    from api.services.exchange_client import interpret_bitget_authorities as f
    r = f([code])
    assert r["can_trade"] is trade and r["can_withdraw"] is False and r["unconfirmed"] == [] and r["extra"] == extra
    assert r["permissions_known"] is True


def test_live_bitget_key_with_all_five_codes_cannot_withdraw():
    from api.services.exchange_client import interpret_bitget_authorities as f
    live = f(["taxw", "p2pw", "wtow", "stow", "smow"])
    assert live == {"can_trade": True, "can_withdraw": False, "permissions_known": True, "unconfirmed": [],
                    "extra": ["p2pw", "smow", "taxw", "wtow"], "read_only": False}
    # any other code (e.g. whatever Bitget uses for Withdraw) still means "withdrawal possible"
    unknown = f(["stow", "wdxx"])
    assert unknown["can_trade"] is True and unknown["can_withdraw"] is None and unknown["unconfirmed"] == ["wdxx"]


async def test_bitget_key_with_confirmed_codes_verifies_with_withdrawal_false_and_extra_warning(db, user, binance_key, client):
    Venue.info = {"can_trade": True, "can_withdraw": False, "permissions": ["taxw", "p2pw", "wtow", "stow", "smow"],
                  "permissions_known": True, "unconfirmed": [], "extra": ["p2pw", "smow", "taxw", "wtow"]}
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["result"] == "ok"
    assert body["withdrawal_enabled"] is False and body["withdrawal_permission_warning"] is False
    assert body["extra_permissions"] == ["p2pw", "smow", "taxw", "wtow"]
    assert "more permissions than AutoBuy needs (p2pw, smow, taxw, wtow)" in body["detail"]
    assert "use a key with only Spot -> Trade" in body["detail"]
    assert body["key"]["permissions"] == ["taxw", "p2pw", "wtow", "stow", "smow"] and body["key"]["extra_permissions"] == ["p2pw", "smow", "taxw", "wtow"]
    await assert_row(db, binance_key, verified=True, status="verified", withdrawal=False, before=before)
    assert binance_key.permissions == "taxw,p2pw,wtow,stow,smow"
    # the stored codes come back on the key list
    async with fresh() as c:
        keys = (await c.get("/api/exchange/keys", headers=auth(user))).json()
    assert next(k for k in keys if k["id"] == str(binance_key.id))["extra_permissions"] == ["p2pw", "smow", "taxw", "wtow"]


async def test_bitget_key_without_stow_fails_with_clear_reason(db, user, binance_key, client):
    Venue.info = {"can_trade": False, "can_withdraw": False, "permissions": ["taxw", "wtow"],
                  "permissions_known": True, "unconfirmed": [], "extra": ["taxw", "wtow"]}
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is False and body["result"] == "no_trade_permission"
    assert body["detail"].startswith("Spot trading permission is required")
    assert "no Spot -> Trade permission" in body["detail"] and "taxw, wtow" in body["detail"]
    await assert_row(db, binance_key, verified=False, status="failed", error_contains="Spot trading permission is required",
                     withdrawal=False, before=before)


async def test_spot_trade_only_key_has_no_extra_warning(db, user, binance_key, client):
    Venue.info = {"can_trade": True, "can_withdraw": False, "permissions": ["stow"], "permissions_known": True,
                  "unconfirmed": [], "extra": []}
    binance_key.exchange = "bitget"
    await db.commit()
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["extra_permissions"] == [] and "more permissions" not in body["detail"]
    assert body["withdrawal_enabled"] is False and body["withdrawal_permission_warning"] is False


async def test_bitget_unconfirmed_code_still_means_withdrawal_possible(db, user, binance_key, client):
    """Whatever code Bitget uses for Withdraw is not confirmed yet: it must never be reported as safe."""
    Venue.info = {"can_trade": True, "can_withdraw": None, "permissions": ["stow", "taxw", "wdxx"],
                  "permissions_known": True, "unconfirmed": ["wdxx"], "extra": ["taxw"]}
    binance_key.exchange = "bitget"
    await db.commit()
    before = binance_key.updated_at
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and body["result"] == "ok"
    assert "treat this key as able to withdraw" in body["detail"] and "wdxx" in body["detail"]
    assert body["withdrawal_enabled"] is None and body["withdrawal_permission_warning"] is True, \
        "unconfirmed codes are never reported as safe"
    assert body["extra_permissions"] == ["taxw"]
    await assert_row(db, binance_key, verified=True, status="verified", withdrawal=None, before=before)


async def test_unknown_codes_only_still_verify(db, user, binance_key, client):
    Venue.info = {"can_trade": None, "can_withdraw": None, "permissions": ["zzz"], "permissions_known": False}
    binance_key.exchange = "bitget"
    await db.commit()
    body = (await verify(client, user, binance_key)).json()
    assert body["verified"] is True and "cannot confirm (zzz)" in body["detail"]
    assert body["withdrawal_permission_warning"] is True
