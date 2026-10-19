"""PUT /api/guardian/accounts/{id}/key (v0.9.2): a customer replacing their own
monitoring key.

The decision is the engine's (`guardian.keyreplacement`): the new key must be
read-only and must belong to the SAME exchange account, or nothing changes.
These tests prove the HTTP side of it: the ownership check first, the
purchase-key refusal before the exchange is asked, 400 with the plain message
and never the reason code, `{ok, message}` on success, and the row and the
event log afterwards. Fixtures, the fake exchange and the row readers are the
ones tests/api/test_guardian_accounts.py defines.
"""
from __future__ import annotations

import hashlib

from api.config import settings
from api.routes.guardian import PURCHASE_KEY_REFUSED
from api.services.crypto import decrypt_value
from tests.api.conftest import auth_headers
from tests.api.test_guardian_accounts import (  # noqa: F401 - fixtures are registered by import
    BODY,
    TRADE,
    UID,
    close_engine_connections,
    events_of,
    exchange,
    fresh,
    guardian_on,
    make_purchase_key,
    other,
    row_of,
    tidy,
)

NEW = {"api_key": "guardian-ro-key-new", "api_secret": "guardian-ro-secret-new", "passphrase": "guardian-pass-new"}


async def onboarded(c, user, **overrides):
    r = await c.post("/api/guardian/accounts", json={**BODY, **overrides}, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_a_read_only_key_for_the_same_exchange_account_replaces_the_old_one(guardian_on, exchange, user, db, tidy):
    exchange.key(NEW["api_key"], uid=UID)
    async with fresh() as c:
        account_id = await onboarded(c, user)
        r = await c.put(f"/api/guardian/accounts/{account_id}/key", json=NEW, headers=auth_headers(user))
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"ok", "message"} and body["ok"] is True
        assert "new key is in place" in body["message"]
        assert "reason_code" not in r.text and "replaced" not in body.get("reason_code", "")
        for secret in (NEW["api_key"], NEW["api_secret"], NEW["passphrase"], BODY["api_key"]):
            assert secret not in r.text
        shown = await c.get(f"/api/guardian/accounts/{account_id}", headers=auth_headers(user))
        assert shown.status_code == 200 and shown.json()["status"] == "active"
    row = await row_of(db, account_id)
    assert decrypt_value(row["encrypted_api_key"]) == NEW["api_key"]
    assert decrypt_value(row["encrypted_secret"]) == NEW["api_secret"]
    assert decrypt_value(row["encrypted_passphrase"]) == NEW["passphrase"]
    assert row["key_fingerprint"] == hashlib.sha256(NEW["api_key"].encode()).hexdigest(), "the fingerprint moves with the key"
    events = await events_of(db, account_id)
    assert [e["event_type"] for e in events] == ["customer_added", "monitoring_key_replaced"]
    assert events[-1]["actor"] == f"user:{user.id}" and events[-1]["payload_json"] == {"exchange": "bitget", "replaced_by": f"user:{user.id}"}
    assert exchange.builds == [BODY["api_key"], BODY["api_key"], NEW["api_key"]], "gate, seed, then the replacement's gate"


async def test_a_replacement_that_can_trade_is_refused_and_nothing_changes(guardian_on, exchange, user, db, tidy):
    exchange.key("guardian-trade-key", authorities=TRADE, uid=UID)
    async with fresh() as c:
        account_id = await onboarded(c, user)
        r = await c.put(f"/api/guardian/accounts/{account_id}/key", json={**NEW, "api_key": "guardian-trade-key"},
                        headers=auth_headers(user))
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "not read-only" in detail and "Nothing was changed" in detail
    assert "reason_code" not in r.text and "not_read_only" not in r.text and "spot_trade_w" not in r.text
    row = await row_of(db, account_id)
    assert decrypt_value(row["encrypted_api_key"]) == BODY["api_key"]
    assert [e["event_type"] for e in await events_of(db, account_id)] == ["customer_added"]


async def test_a_read_only_key_for_a_different_exchange_account_is_refused(guardian_on, exchange, user, db, tidy):
    """The case the engine module is shaped around: a perfectly good key for
    the wrong account would leave the real one unwatched while reporting
    'watching'."""
    exchange.key("guardian-wrong-account-key", uid="99999")
    async with fresh() as c:
        account_id = await onboarded(c, user)
        r = await c.put(f"/api/guardian/accounts/{account_id}/key", json={**NEW, "api_key": "guardian-wrong-account-key"},
                        headers=auth_headers(user))
    assert r.status_code == 400, r.text
    assert "different exchange account" in r.json()["detail"]
    assert "wrong_account" not in r.text and "99999" not in r.text and UID not in r.text
    row = await row_of(db, account_id)
    assert decrypt_value(row["encrypted_api_key"]) == BODY["api_key"]
    assert row["key_fingerprint"] == hashlib.sha256(BODY["api_key"].encode()).hexdigest()


async def test_a_blank_key_or_secret_is_refused_before_the_exchange_is_asked(guardian_on, exchange, user, db, tidy):
    async with fresh() as c:
        account_id = await onboarded(c, user)
        built = list(exchange.builds)
        for body in ({"api_key": "   ", "api_secret": "s"}, {"api_key": "k", "api_secret": ""}, {"api_secret": "s"},
                     {"api_key": "k", "api_secret": "s", "label": "x"}):
            r = await c.put(f"/api/guardian/accounts/{account_id}/key", json=body, headers=auth_headers(user))
            assert r.status_code == 422, (body, r.text)
    assert exchange.builds == built
    assert decrypt_value((await row_of(db, account_id))["encrypted_api_key"]) == BODY["api_key"]


async def test_a_purchase_key_is_refused_as_a_replacement_before_the_exchange_is_asked(guardian_on, exchange, user, db, tidy):
    await make_purchase_key(db, user, "guardian-purchase-key")
    async with fresh() as c:
        account_id = await onboarded(c, user)
        built = list(exchange.builds)
        r = await c.put(f"/api/guardian/accounts/{account_id}/key", json={**NEW, "api_key": "guardian-purchase-key"},
                        headers=auth_headers(user))
    assert (r.status_code, r.json()["detail"]) == (400, PURCHASE_KEY_REFUSED)
    assert exchange.builds == built
    assert decrypt_value((await row_of(db, account_id))["encrypted_api_key"]) == BODY["api_key"]


async def test_another_users_account_and_an_unknown_id_are_404(guardian_on, exchange, user, other, db, tidy):
    exchange.key("guardian-other-key", uid="55555")
    async with fresh() as c:
        theirs = await onboarded(c, other, api_key="guardian-other-key", name="Theirs")
        built = list(exchange.builds)
        r = await c.put(f"/api/guardian/accounts/{theirs}/key", json=NEW, headers=auth_headers(user))
        assert (r.status_code, r.json()) == (404, {"detail": "Not found"})
        r = await c.put("/api/guardian/accounts/999999999/key", json=NEW, headers=auth_headers(user))
        assert (r.status_code, r.json()) == (404, {"detail": "Not found"})
    assert exchange.builds == built
    assert decrypt_value((await row_of(db, theirs))["encrypted_api_key"]) == "guardian-other-key"


async def test_404_while_the_flag_is_off_even_for_the_owner(exchange, user, other, db, tidy, monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
    async with fresh() as c:
        account_id = await onboarded(c, user)
        monkeypatch.setattr(settings, "guardian_enabled", False)
        r = await c.put(f"/api/guardian/accounts/{account_id}/key", json=NEW, headers=auth_headers(user))
    assert (r.status_code, r.json()) == (404, {"detail": "Not found"})
    assert decrypt_value((await row_of(db, account_id))["encrypted_api_key"]) == BODY["api_key"]


async def test_replacing_is_limited_to_three_a_minute(guardian_on, exchange, user, db, tidy):
    from api.services.rate_limit import limiter
    exchange.key(NEW["api_key"], uid=UID)
    async with fresh() as c:
        account_id = await onboarded(c, user)
        limiter.reset()
        limiter.enabled = True    # off under tests by default, as in test_rate_limit.py
        try:
            codes = [(await c.put(f"/api/guardian/accounts/{account_id}/key", json=NEW, headers=auth_headers(user))).status_code
                     for _ in range(4)]
        finally:
            limiter.enabled = False
            limiter.reset()
    assert codes == [200, 200, 200, 429], "re-submitting the same key in place is allowed; the fourth call in a minute is not"
