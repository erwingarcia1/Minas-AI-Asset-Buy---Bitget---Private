"""/api/guardian/accounts (v0.9.2): a customer's monitored accounts through the API.

The engine (guardian/) owns every rule; these tests prove the HTTP mapping:
404 while the feature is off or hidden from the caller, the create path's
refusals in their order, the response shape (never key material), the
background baseline seed, and that every path is scoped to the caller's own
accounts. The exchange is a fake: `guardian.onboarding.make_client` (and the
key-replacement module's copy of it) is replaced with the mocked BitgetClient
the engine's own onboarding tests use, so nothing here reaches a live venue.

The API writes through the engine's own synchronous connection; rows are read
back here through the async session on the same test database. Every
guardian_* row made for the test users is removed afterwards, and the removal
is checked.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import text

from api.config import settings
from api.models import APIKey, User
from api.routes.guardian import (
    KEY_ALREADY_MONITORED,
    MAX_ACCOUNTS,
    NOTHING_TO_UPDATE,
    PURCHASE_KEY_REFUSED,
    TOO_MANY_ACCOUNTS,
)
from api.schemas.guardian import PASSPHRASE_REQUIRED, SECRET_COLUMNS, GuardianAccountCreate, GuardianAccountOut, account_out
from api.services import guardian_bridge
from api.services.crypto import decrypt_value, encrypt_value
from guardian import keyreplacement as engine_keyreplacement
from guardian import onboarding as engine_onboarding
from guardian.onboarding import ALREADY_MONITORED_MESSAGE, NOT_READ_ONLY_MESSAGE
from tests.api.conftest import auth_headers
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.test_onboarding import _client_with

# Bitget authority codes confirmed on live keys (api/services/bitget_permissions.py): the
# read-only gate accepts only confirmed codes, so a made-up one is refused by name.
READ_ONLY = "stor"          # Spot -> Read
TRADE = "stor,stow"         # Spot -> Read plus Spot -> Trade
UID = "12345"

BODY = {"exchange": "bitget", "api_key": "guardian-ro-key-1", "api_secret": "guardian-ro-secret-1",
        "passphrase": "guardian-pass-1", "name": "Main Bitget account"}
KEY_BODY = {"api_key": "guardian-replacement-key", "api_secret": "guardian-replacement-secret"}

# Every route of the router, with a valid body where one is needed. `{id}` is the account id.
ALL_PATHS = [
    ("POST", "/api/guardian/accounts", BODY),
    ("GET", "/api/guardian/accounts", None),
    ("GET", "/api/guardian/accounts/{id}", None),
    ("PUT", "/api/guardian/accounts/{id}/key", KEY_BODY),
    ("PATCH", "/api/guardian/accounts/{id}", {"alert_phone": "+1 555 010 0100"}),
    ("POST", "/api/guardian/accounts/{id}/disable", None),
    ("POST", "/api/guardian/accounts/{id}/enable", None),
    ("DELETE", "/api/guardian/accounts/{id}", None),
]
PER_ACCOUNT_PATHS = ALL_PATHS[2:]


def fresh():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def hit(c, method, path, body, user, account_id=1):
    return await c.request(method, path.format(id=account_id), json=body, headers=auth_headers(user))


class FakeExchange:
    """What the engine's `make_client` hands back, per api_key: the mocked
    BitgetClient of tests/guardian/test_onboarding.py answering canned
    payloads. `authorities` decides the read-only verdict and `uid` the
    exchange account the key belongs to; `fail_seed` makes the SECOND build
    of a key (the one seeding does) raise, and `history_error` makes the
    order history unreadable, which is a partial baseline. `builds` records
    every key a client was built for, so a test can prove the exchange was
    not asked."""

    def __init__(self):
        self.specs = {}
        self.builds = []

    def key(self, api_key, *, authorities=READ_ONLY, uid=UID, fail_seed=False, history_error=None):
        self.specs[api_key] = dict(authorities=authorities, uid=uid, fail_seed=fail_seed, history_error=history_error)

    def make_client(self, exchange, api_key, secret, passphrase=""):
        spec = self.specs.get(api_key) or dict(authorities=READ_ONLY, uid=UID, fail_seed=False, history_error=None)
        self.builds.append(api_key)
        if spec["fail_seed"] and self.builds.count(api_key) > 1:
            raise RuntimeError("exchange unreachable")
        payload = account_info_response(authorities=spec["authorities"])
        payload["data"]["userId"] = spec["uid"]
        client = _client_with(account_info_result=payload)
        if spec["history_error"] is not None:
            client.fetch_closed_orders = MagicMock(side_effect=spec["history_error"])
        return client


@pytest.fixture
def exchange(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(engine_onboarding, "make_client", fake.make_client)
    monkeypatch.setattr(engine_keyreplacement, "make_client", fake.make_client)
    return fake


@pytest.fixture
def guardian_on(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")


@pytest.fixture(autouse=True)
def close_engine_connections():
    """The bridge's Database keeps one psycopg2 connection per worker thread,
    and each test runs on a fresh event loop with a fresh thread pool, which
    would strand those connections. Closed after every test so the test
    Postgres never runs out of connections."""
    yield
    guardian_bridge.close()


@pytest.fixture
async def other(db):
    """A second customer, for the ownership checks."""
    u = User(email=f"svc-other-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    from sqlalchemy import select
    found = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if found is not None:
        await db.delete(found)
        await db.commit()


@pytest.fixture
async def tidy(db, user, other):
    """Every guardian_accounts row of the two test users (and, by cascade,
    its events and poll state), and every operator page about one of those
    accounts, is removed after the test, whatever the engine wrote through
    its own connection, and the removal is checked. Pages go first: their
    account_id is ON DELETE SET NULL, so removing the account would only
    orphan them."""
    yield
    await db.rollback()
    for u in (user, other):
        await db.execute(text("DELETE FROM guardian_operator_pages WHERE account_id IN "
                              "(SELECT id FROM guardian_accounts WHERE user_id = :uid)"), {"uid": u.id})
        await db.execute(text("DELETE FROM guardian_accounts WHERE user_id = :uid"), {"uid": u.id})
    await db.commit()
    left = (await db.execute(text("SELECT count(*) FROM guardian_accounts WHERE user_id IN (:a, :b)"),
                             {"a": user.id, "b": other.id})).scalar_one()
    await db.commit()
    assert left == 0, f"{left} guardian_accounts rows left behind by the test"


async def row_of(db, account_id):
    found = (await db.execute(text("SELECT * FROM guardian_accounts WHERE id = :id"), {"id": account_id})).mappings().one_or_none()
    await db.commit()
    return dict(found) if found is not None else None


async def events_of(db, account_id):
    rows = (await db.execute(text("SELECT event_type, severity, payload_json, actor FROM guardian_events "
                                  "WHERE account_id = :id ORDER BY id"), {"id": account_id})).mappings().all()
    await db.commit()
    out = []
    for r in rows:
        payload = r["payload_json"]
        out.append({**dict(r), "payload_json": json.loads(payload) if isinstance(payload, str) else payload})
    return out


async def count_for(db, user):
    n = (await db.execute(text("SELECT count(*) FROM guardian_accounts WHERE user_id = :uid"), {"uid": user.id})).scalar_one()
    await db.commit()
    return n


async def pages_of(db, account_id):
    """The operator pages about one account, oldest first: what the engine
    recorded for Minas through the bridge's pager."""
    rows = (await db.execute(text("SELECT rule, severity, message, evidence_json, delivered_to FROM guardian_operator_pages "
                                  "WHERE account_id = :id ORDER BY id"), {"id": account_id})).mappings().all()
    await db.commit()
    out = []
    for r in rows:
        evidence = r["evidence_json"]
        out.append({**dict(r), "evidence_json": json.loads(evidence) if isinstance(evidence, str) else evidence})
    return out


async def make_purchase_key(db, user, api_key):
    k = APIKey(user_id=user.id, exchange="bitget", encrypted_api_key=encrypt_value(api_key),
               encrypted_api_secret=encrypt_value("purchase-secret"), encrypted_passphrase=encrypt_value("purchase-pass"),
               verified=True)
    db.add(k)
    await db.commit()


async def plant_accounts(db, user, n, *, removed=False):
    """Rows as the engine leaves them, without going through the exchange.
    `removed=True` plants a DELETE'd account: switched off, no key material."""
    for _ in range(n):
        await db.execute(text(
            "INSERT INTO guardian_accounts (user_id, label, exchange, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, key_fingerprint, status, baseline_status, onboarding_source, onboarded_at, updated_at) "
            "VALUES (:uid, :label, 'bitget', 'independent', :k, :s, :p, :fp, :status, 'complete', 'self', now(), now())"),
            {"uid": user.id, "label": f"planted-{uuid.uuid4().hex[:10]}",
             "k": "" if removed else "enc-k", "s": "" if removed else "enc-s", "p": "" if removed else "enc-p",
             "fp": None if removed else uuid.uuid4().hex, "status": "disabled" if removed else "active"})
    await db.commit()


# --- the feature flag and the allowlist ---------------------------------------


async def test_every_path_answers_404_while_the_flag_is_off(user, exchange, monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", False)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
    async with fresh() as c:
        for method, path, body in ALL_PATHS:
            for account_id in (1, 999_999_999):
                r = await hit(c, method, path, body, user, account_id)
                assert (r.status_code, r.json()) == (404, {"detail": "Not found"}), (method, path, r.text)
    assert exchange.builds == [], "the exchange is never contacted while the feature is off"


async def test_every_path_answers_404_when_the_caller_is_not_on_the_allowlist(user, exchange, monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "someone-else@example.com")
    async with fresh() as c:
        for method, path, body in ALL_PATHS:
            r = await hit(c, method, path, body, user)
            assert (r.status_code, r.json()) == (404, {"detail": "Not found"}), (method, path, r.text)
        # the allowlist is the only difference: on it (compared lower-case), the same caller gets through
        monkeypatch.setattr(settings, "guardian_allowed_emails_csv", f"someone-else@example.com, {user.email.upper()}")
        r = await c.get("/api/guardian/accounts", headers=auth_headers(user))
        assert (r.status_code, r.json()) == (200, [])
    assert exchange.builds == []


async def test_while_the_flag_is_off_an_anonymous_caller_gets_404_like_a_missing_path(exchange, monkeypatch):
    """The flag is checked before the credentials: nobody is asked to sign in
    for a feature that is off, and a probe cannot tell it from a path that
    does not exist."""
    monkeypatch.setattr(settings, "guardian_enabled", False)
    async with fresh() as c:
        missing = await c.get("/api/guardian/nothing-here")
        assert missing.status_code == 404
        for method, path, body in ALL_PATHS:
            r = await c.request(method, path.format(id=1), json=body)
            assert (r.status_code, r.json()) == (404, {"detail": "Not found"}), (method, path, r.text)
            assert "www-authenticate" not in r.headers, "no request to sign in for a feature that is off"
    assert exchange.builds == []


async def test_with_the_flag_on_an_anonymous_caller_gets_401_on_every_path(guardian_on, exchange):
    async with fresh() as c:
        for method, path, body in ALL_PATHS:
            r = await c.request(method, path.format(id=1), json=body)
            assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer", (method, path, r.text)
    assert exchange.builds == []


def test_the_path_lists_cover_every_route_of_the_router():
    """The 404 and 401 sweeps above are only as good as these lists: every
    route registered on the router is in ALL_PATHS, and every per-account one
    in PER_ACCOUNT_PATHS."""
    from api.routes.guardian import router
    registered = {(method, "/api/guardian" + route.path) for route in router.routes for method in route.methods}
    listed = {(method, path.replace("{id}", "{account_id}")) for method, path, _ in ALL_PATHS}
    assert listed == registered
    assert PER_ACCOUNT_PATHS == [entry for entry in ALL_PATHS if "{id}" in entry[1]]


# --- adding an account ---------------------------------------------------------


async def test_adding_an_account_answers_201_with_the_account_and_no_key_material(guardian_on, exchange, user, db, tidy):
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == set(GuardianAccountOut.model_fields)
        assert body["name"] == "Main Bitget account" and body["label"] == "main-bitget-account"
        assert body["exchange"] == "bitget" and body["topology"] == "independent" and body["account_mode"] == "classic"
        assert body["status"] == "active"
        assert body["baseline_status"] == "seeding", "the 201 is answered before the background seed runs"
        assert body["exchange_uid"] == UID and body["sms_alerts_enabled"] is False and body["last_success_at"] is None
        assert body["created_at"].endswith(("Z", "+00:00")) and body["updated_at"].endswith(("Z", "+00:00")), "UTC, and says so"
        for secret in (BODY["api_key"], BODY["api_secret"], BODY["passphrase"], "encrypted", "fingerprint"):
            assert secret not in r.text
        shown = await c.get(f"/api/guardian/accounts/{body['id']}", headers=auth_headers(user))
    assert shown.status_code == 200 and shown.json()["baseline_status"] == "complete", \
        "the background seed ran (and finished) before the response was handed back"
    row = await row_of(db, body["id"])
    assert decrypt_value(row["encrypted_api_key"]) == BODY["api_key"]
    assert decrypt_value(row["encrypted_secret"]) == BODY["api_secret"]
    assert decrypt_value(row["encrypted_passphrase"]) == BODY["passphrase"]
    assert row["key_fingerprint"] == hashlib.sha256(BODY["api_key"].encode()).hexdigest()
    assert row["onboarding_source"] == "self" and row["onboarded_by"] is None and row["user_id"] == user.id
    assert row["customer_name"] == "Main Bitget account"
    events = await events_of(db, body["id"])
    assert [e["event_type"] for e in events] == ["customer_added"]
    assert events[0]["actor"] == f"user:{user.id}"
    assert exchange.builds == [BODY["api_key"]] * 2, "one client for the read-only gate, one for the seed"
    assert await pages_of(db, body["id"]) == [], "a clean onboarding pages nobody"


async def test_contact_fields_and_the_sms_switch_are_stored_at_creation(guardian_on, exchange, user, db, tidy):
    body = {**BODY, "sms_alerts_enabled": True, "alert_phone": "+1 555 010 0100", "alert_email_secondary": "backup@example.com",
            "alert_phone_secondary": "", "telegram_handle": "  @backup  ", "topology": "sub_account"}
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json=body, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["sms_alerts_enabled"] is True and out["alert_phone"] == "+1 555 010 0100"
    assert out["alert_email_secondary"] == "backup@example.com" and out["telegram_handle"] == "@backup"
    assert out["alert_phone_secondary"] is None, "a blank input is stored as nothing, not as ''"
    assert out["topology"] == "sub_account"


async def test_a_partial_baseline_is_recorded_as_incomplete_with_the_gap(guardian_on, exchange, user, db, tidy):
    exchange.key(BODY["api_key"], history_error=RuntimeError("order history endpoint is down"))
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    row = await row_of(db, r.json()["id"])
    assert row["baseline_status"] == "incomplete"
    events = await events_of(db, row["id"])
    gaps = [e for e in events if e["event_type"] == "baseline_seed_incomplete"]
    assert len(gaps) == 1 and gaps[0]["payload_json"]["what"] == "closed_orders"
    # Minas is told, through the table the worker writes: the engine pages through the bridge's pager
    pages = await pages_of(db, row["id"])
    assert [p["rule"] for p in pages] == ["account_onboarded_with_partial_baseline"] and pages[0]["severity"] == "HIGH"
    assert pages[0]["evidence_json"]["gaps"][0]["what"] == "closed_orders" and pages[0]["delivered_to"] == 0


async def test_a_failed_seed_never_leaves_the_row_seeding(guardian_on, exchange, user, db, tidy):
    """The gate passes (first client), seeding cannot even build a client
    (second): the row is marked incomplete with a redacted reason, so the
    engine polls it instead of skipping a row that says 'seeding' forever."""
    exchange.key(BODY["api_key"], fail_seed=True)
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    row = await row_of(db, r.json()["id"])
    assert row["baseline_status"] == "incomplete"
    events = await events_of(db, row["id"])
    failed = [e for e in events if e["event_type"] == "baseline_seed_failed"]
    assert len(failed) == 1 and "RuntimeError" in failed[0]["payload_json"]["error"]
    for secret in (BODY["api_key"], BODY["api_secret"], BODY["passphrase"]):
        assert secret not in json.dumps(events)
    pages = await pages_of(db, row["id"])
    assert [p["rule"] for p in pages] == ["account_baseline_seed_failed"] and pages[0]["severity"] == "HIGH"
    assert "Re-run the seed" in pages[0]["message"]
    for secret in (BODY["api_key"], BODY["api_secret"], BODY["passphrase"]):
        assert secret not in json.dumps(pages)


async def test_a_purchase_key_is_refused_before_the_exchange_is_asked(guardian_on, exchange, user, db, tidy):
    await make_purchase_key(db, user, BODY["api_key"])
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
        assert (r.status_code, r.json()["detail"]) == (400, PURCHASE_KEY_REFUSED)
        assert exchange.builds == [] and await count_for(db, user) == 0
        # the refusal is about that key: a different, read-only one goes through
        r = await c.post("/api/guardian/accounts", json={**BODY, "api_key": "guardian-ro-key-2"}, headers=auth_headers(user))
        assert r.status_code == 201, r.text


async def test_the_eleventh_account_is_refused_and_removed_ones_do_not_count(guardian_on, exchange, user, db, tidy):
    await plant_accounts(db, user, MAX_ACCOUNTS - 1)
    await plant_accounts(db, user, 1, removed=True)
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
        assert r.status_code == 201, (r.text, "a removed account has no key and does not count")
        r = await c.post("/api/guardian/accounts", json={**BODY, "api_key": "guardian-ro-key-2", "name": "Second"},
                         headers=auth_headers(user))
        assert (r.status_code, r.json()["detail"]) == (400, TOO_MANY_ACCOUNTS)
    assert "guardian-ro-key-2" not in exchange.builds, "the cap is checked before the exchange is asked"
    assert await count_for(db, user) == MAX_ACCOUNTS + 1   # ten live, one removed


async def test_the_same_exchange_account_is_refused_with_409(guardian_on, exchange, user, db, tidy):
    exchange.key("guardian-ro-key-2", uid=UID)
    exchange.key("guardian-ro-key-3", uid="67890")
    async with fresh() as c:
        first = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
        assert first.status_code == 201
        again = await c.post("/api/guardian/accounts", json={**BODY, "api_key": "guardian-ro-key-2", "name": "Backup"},
                             headers=auth_headers(user))
        assert again.status_code == 409, again.text
        assert again.json()["detail"] == ALREADY_MONITORED_MESSAGE.format(label="main-bitget-account")
        assert await count_for(db, user) == 1
        # a different exchange account under the same name is a second account with its own label
        third = await c.post("/api/guardian/accounts", json={**BODY, "api_key": "guardian-ro-key-3"}, headers=auth_headers(user))
        assert third.status_code == 201 and third.json()["label"] == "main-bitget-account-2"


async def test_a_key_held_by_a_switched_off_account_is_refused_with_409(guardian_on, exchange, user, db, tidy):
    """The duplicate check skips disabled rows (a customer may re-add an account
    they switched off, with a fresh key); the same KEY is still refused, by the
    unique fingerprint, and the customer reads a sentence, not a constraint."""
    async with fresh() as c:
        first = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
        account_id = first.json()["id"]
        off = await c.post(f"/api/guardian/accounts/{account_id}/disable", headers=auth_headers(user))
        assert off.status_code == 200 and off.json()["status"] == "disabled"
        again = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
    assert (again.status_code, again.json()["detail"]) == (409, KEY_ALREADY_MONITORED)
    assert "uq_guardian" not in again.text and await count_for(db, user) == 1


async def test_a_key_that_can_trade_is_refused_with_the_plain_message(guardian_on, exchange, user, db, tidy):
    exchange.key("guardian-trade-key", authorities=TRADE)
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json={**BODY, "api_key": "guardian-trade-key"}, headers=auth_headers(user))
    assert (r.status_code, r.json()["detail"]) == (400, NOT_READ_ONLY_MESSAGE)
    assert "'stow'" not in r.text and "authorit" not in r.text, "the technical reason stays in the log"
    assert await count_for(db, user) == 0


async def test_a_bitget_or_okx_key_without_its_passphrase_is_refused_before_the_exchange_is_asked(guardian_on, exchange, user, db, tidy):
    """Without the passphrase the exchange refuses every signed request, which
    the engine could only report as "not read-only". The body says what is
    missing, in the customer's words, and the key is never echoed."""
    without = {k: v for k, v in BODY.items() if k != "passphrase"}
    async with fresh() as c:
        for body in (without, {**without, "passphrase": ""}, {**without, "passphrase": "   "}, {**without, "exchange": "okx"}):
            r = await c.post("/api/guardian/accounts", json=body, headers=auth_headers(user))
            assert r.status_code == 422, (body, r.text)
            errors = r.json()["detail"]
            assert [e["msg"] for e in errors] == [PASSPHRASE_REQUIRED] and errors[0]["loc"] == ["body", "passphrase"], errors
            assert BODY["api_key"] not in r.text and BODY["api_secret"] not in r.text
    assert exchange.builds == [] and await count_for(db, user) == 0
    # the schema knows which exchanges: a Bybit or Binance key has no passphrase and passes without one
    for exchange_name in ("bybit", "binance"):
        assert GuardianAccountCreate(**{**without, "exchange": exchange_name}).passphrase is None


async def test_adding_is_limited_to_three_a_minute(guardian_on, exchange, user, db, tidy):
    from api.services.rate_limit import limiter
    limiter.reset()
    limiter.enabled = True    # off under tests by default, as in test_rate_limit.py
    try:
        async with fresh() as c:
            codes = [(await c.post("/api/guardian/accounts", json={**BODY, "api_key": f"guardian-ro-key-{i}"},
                                   headers=auth_headers(user))).status_code for i in range(4)]
    finally:
        limiter.enabled = False
        limiter.reset()
    assert codes == [201, 409, 409, 429]


# --- reading and acting on accounts ---------------------------------------------


async def test_accounts_are_scoped_to_the_caller_on_every_path(guardian_on, exchange, user, other, db, tidy):
    exchange.key("guardian-other-key", uid="99999")
    async with fresh() as c:
        mine = (await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))).json()
        theirs = (await c.post("/api/guardian/accounts", json={**BODY, "api_key": "guardian-other-key", "name": "Theirs"},
                               headers=auth_headers(other))).json()
        listing = await c.get("/api/guardian/accounts", headers=auth_headers(user))
        assert listing.status_code == 200 and [a["id"] for a in listing.json()] == [mine["id"]]
        assert (await c.get(f"/api/guardian/accounts/{mine['id']}", headers=auth_headers(user))).status_code == 200
        for method, path, body in PER_ACCOUNT_PATHS:
            r = await hit(c, method, path, body, user, theirs["id"])
            assert (r.status_code, r.json()) == (404, {"detail": "Not found"}), (method, path, r.text)
        for method, path, body in PER_ACCOUNT_PATHS:
            r = await hit(c, method, path, body, user, 999_999_999)
            assert (r.status_code, r.json()) == (404, {"detail": "Not found"}), (method, path, r.text)
    row = await row_of(db, theirs["id"])
    assert row["status"] == "active" and decrypt_value(row["encrypted_api_key"]) == "guardian-other-key"
    assert [e["event_type"] for e in await events_of(db, theirs["id"])] == ["customer_added"], "nothing happened to it"


async def test_patch_changes_contacts_and_the_sms_switch_and_records_only_field_names(guardian_on, exchange, user, db, tidy):
    async with fresh() as c:
        account_id = (await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))).json()["id"]
        r = await c.patch(f"/api/guardian/accounts/{account_id}",
                          json={"alert_phone": "+1 555 010 0100", "sms_alerts_enabled": True,
                                "alert_email_secondary": "Backup@Example.com"},
                          headers=auth_headers(user))
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["alert_phone"] == "+1 555 010 0100" and out["sms_alerts_enabled"] is True
        assert out["alert_email_secondary"] == "backup@example.com"
        events = await events_of(db, account_id)
        assert events[-1]["event_type"] == "alert_contact_updated"
        assert events[-1]["payload_json"] == {"fields": ["alert_email_secondary", "alert_phone", "sms_alerts_enabled"]}
        assert "555" not in json.dumps(events) and "backup@" not in json.dumps(events).lower(), "field names only, never values"
        assert events[-1]["actor"] == f"user:{user.id}"
        # null clears a field; an absent one is untouched
        r = await c.patch(f"/api/guardian/accounts/{account_id}", json={"alert_phone": None}, headers=auth_headers(user))
        assert r.status_code == 200 and r.json()["alert_phone"] is None and r.json()["sms_alerts_enabled"] is True
        # nothing to change, an unknown field, a bad address, a value that does not fit
        assert (await c.patch(f"/api/guardian/accounts/{account_id}", json={}, headers=auth_headers(user))).json() == {"detail": NOTHING_TO_UPDATE}
        assert (await c.patch(f"/api/guardian/accounts/{account_id}", json={"label": "renamed"}, headers=auth_headers(user))).status_code == 422
        assert (await c.patch(f"/api/guardian/accounts/{account_id}", json={"alert_email_secondary": "not-an-email"}, headers=auth_headers(user))).status_code == 422
        assert (await c.patch(f"/api/guardian/accounts/{account_id}", json={"alert_phone": "9" * 33}, headers=auth_headers(user))).status_code == 422
    row = await row_of(db, account_id)
    assert row["label"] == "main-bitget-account" and row["alert_phone"] is None and row["sms_alerts_enabled"] is True


async def test_disable_and_enable_switch_monitoring_and_are_recorded(guardian_on, exchange, user, db, tidy):
    async with fresh() as c:
        account_id = (await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))).json()["id"]
        off = await c.post(f"/api/guardian/accounts/{account_id}/disable", headers=auth_headers(user))
        assert off.status_code == 200 and off.json()["status"] == "disabled"
        assert (await row_of(db, account_id))["status"] == "disabled"
        again = await c.post(f"/api/guardian/accounts/{account_id}/disable", headers=auth_headers(user))
        assert again.status_code == 200 and again.json()["status"] == "disabled", "switching off twice is not an error"
        on = await c.post(f"/api/guardian/accounts/{account_id}/enable", headers=auth_headers(user))
        assert on.status_code == 200 and on.json()["status"] == "active"
        listing = (await c.get("/api/guardian/accounts", headers=auth_headers(user))).json()
        assert [a["status"] for a in listing] == ["active"]
    kinds = [e["event_type"] for e in await events_of(db, account_id)]
    assert kinds == ["customer_added", "account_disabled", "account_enabled"], "one event per change, none for the no-op"
    assert decrypt_value((await row_of(db, account_id))["encrypted_api_key"]) == BODY["api_key"], "the key stays for the switch back on"


async def test_delete_scrubs_the_key_and_keeps_the_history(guardian_on, exchange, user, db, tidy):
    async with fresh() as c:
        account_id = (await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))).json()["id"]
        gone = await c.delete(f"/api/guardian/accounts/{account_id}", headers=auth_headers(user))
        assert gone.status_code == 204 and gone.content == b""
        row = await row_of(db, account_id)
        assert row is not None, "the row is kept: its alerts and events are the customer's record"
        assert row["status"] == "disabled"
        assert (row["encrypted_api_key"], row["encrypted_secret"], row["encrypted_passphrase"]) == ("", "", "")
        assert row["key_fingerprint"] is None and row["label"] == "main-bitget-account"
        events = await events_of(db, account_id)
        assert [e["event_type"] for e in events] == ["customer_added", "account_deleted"]
        assert events[-1]["payload_json"] == {"label": "main-bitget-account", "exchange": "bitget", "previous_status": "active"}
        # removed means no longer the customer's to see or act on: 404 like anybody else's, and not listed
        assert (await c.get(f"/api/guardian/accounts/{account_id}", headers=auth_headers(user))).status_code == 404
        assert (await c.post(f"/api/guardian/accounts/{account_id}/enable", headers=auth_headers(user))).status_code == 404
        assert (await c.get("/api/guardian/accounts", headers=auth_headers(user))).json() == []
        # the fingerprint was cleared, so the same key may be added again, as a new account
        readded = await c.post("/api/guardian/accounts", json=BODY, headers=auth_headers(user))
        assert readded.status_code == 201, readded.text
        assert readded.json()["id"] != account_id and readded.json()["label"] == "main-bitget-account-2"


# --- the response model -----------------------------------------------------------


def test_the_response_model_has_no_field_for_key_material():
    assert SECRET_COLUMNS.isdisjoint(GuardianAccountOut.model_fields)
    row = {
        "id": 7, "user_id": uuid.uuid4(), "label": "acct", "customer_name": "Acct", "exchange": "bitget",
        "topology": "independent", "account_mode": "classic", "encrypted_api_key": "gAAAA-the-key",
        "encrypted_secret": "gAAAA-the-secret", "encrypted_passphrase": "gAAAA-the-passphrase",
        "key_fingerprint": "f" * 64, "alert_phone": None, "alert_email_secondary": None, "alert_phone_secondary": None,
        "telegram_handle": None, "telegram_handle_secondary": "@second", "license": "L-1", "sms_alerts_enabled": True,
        "status": "active", "exchange_uid": "12345", "baseline_status": "complete", "onboarding_source": "self",
        "onboarded_by": None, "onboarded_at": "2026-10-19T10:00:00+00:00", "updated_at": "2026-10-19T10:00:00+00:00",
        "last_success_at": None,
    }
    out = account_out(row)
    dumped = out.model_dump_json()
    for column in SECRET_COLUMNS:
        assert column not in dumped and row[column] not in dumped
    for private in ("user_id", "onboarded_by", "onboarding_source", "license", "telegram_handle_secondary", "@second", "L-1"):
        assert private not in dumped
    assert out.name == "Acct" and out.created_at.tzinfo is not None and out.created_at.utcoffset().total_seconds() == 0


def test_the_exchange_choices_match_the_engines():
    from typing import get_args

    from api.schemas.guardian import GuardianExchange
    from guardian.exchange import SUPPORTED_EXCHANGES
    assert set(get_args(GuardianExchange)) == set(SUPPORTED_EXCHANGES)


async def test_the_body_is_validated_without_echoing_the_key(guardian_on, exchange, user, tidy):
    async with fresh() as c:
        r = await c.post("/api/guardian/accounts", json={**BODY, "exchange": "kraken"}, headers=auth_headers(user))
        assert r.status_code == 422 and BODY["api_key"] not in r.text and BODY["api_secret"] not in r.text
        r = await c.post("/api/guardian/accounts", json={**BODY, "name": "   "}, headers=auth_headers(user))
        assert r.status_code == 422
        r = await c.post("/api/guardian/accounts", json={**BODY, "api_key": "k" * 257}, headers=auth_headers(user))
        assert r.status_code == 422
        r = await c.post("/api/guardian/accounts", json={**BODY, "status": "active"}, headers=auth_headers(user))
        assert r.status_code == 422, "fields the body does not know are refused, not ignored"
    assert exchange.builds == []


async def test_a_wrong_method_probe_is_404_while_the_flag_is_off(monkeypatch):
    """A method the collection does not support would answer 405 with an Allow header before any
    dependency runs, revealing the routes exist; while the flag is off it is 404 like a missing path."""
    monkeypatch.setattr(settings, "guardian_enabled", False)
    async with fresh() as c:
        r = await c.delete("/api/guardian/accounts")
    assert r.status_code == 404 and "allow" not in {k.lower() for k in r.headers}
    assert r.json() == {"detail": "Not found"}
    monkeypatch.setattr(settings, "guardian_enabled", True)
    async with fresh() as c:
        r = await c.delete("/api/guardian/accounts")
    assert r.status_code == 405, "with the feature on, an unsupported method is reported as such"
