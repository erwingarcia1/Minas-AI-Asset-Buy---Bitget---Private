"""v0.7.7: the daily read-only check that catches accounts migrated to Unified Trading mode."""
import pytest

from api.services import key_verification as kv
from api.services import telegram, unified_check as uc
from api.services.exchange_client import ExchangeClientError, interpret_bitget_authorities
from tests.conftest_autobuy import make_key, make_purchase

pytestmark = pytest.mark.asyncio
UNIFIED = ExchangeClientError("Bitget error 40085: You are in Unified Account mode, and the Classic Account API is not supported at this time", "40085")


class Venue:
    """Fake venue client keyed by api key: answers get_account_info per key, never touches an order path."""
    answers: dict = {}
    calls: list = []

    def __init__(self, api_key, api_secret, passphrase="", exchange="bitget"):
        self.api_key = api_key

    def get_account_info(self):
        Venue.calls.append(self.api_key)
        a = Venue.answers.get(self.api_key, "ok")
        if isinstance(a, Exception):
            raise a
        return {**interpret_bitget_authorities(["stow"]), "permissions": ["stow"]}


class CapturingEmail:
    def __init__(self):
        self.sent = []

    async def queue(self, to, subject, html, text, template="generic", category="confirmation", user_id=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "template": template, "category": category, "user_id": str(user_id)})
        return True


@pytest.fixture(autouse=True)
def venue(monkeypatch):
    Venue.answers, Venue.calls = {}, []
    monkeypatch.setattr(kv, "ExchangeClient", Venue)
    yield


@pytest.fixture
def mail(monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(uc, "email_service", capture)
    return capture


@pytest.fixture
def tg(monkeypatch):
    sent = []
    async def fake_send(text, key=None):
        sent.append((text, key)); return True
    monkeypatch.setattr(telegram, "send", fake_send)
    return sent


async def plain(db, key):
    from api.routes.exchange import decrypt_value
    return decrypt_value(key.encrypted_api_key)


async def test_a_key_that_turned_unified_is_flagged_once_and_a_classic_key_is_left_alone(db, user, mail, tg, client):
    from api.routes.exchange import encrypt_value
    classic = await make_key(db, user, exchange="bitget")
    unified = await make_key(db, user, exchange="bitget")
    classic.encrypted_api_key, unified.encrypted_api_key = encrypt_value("classic-key-1"), encrypt_value("unified-key-1")
    for k in (classic, unified):
        k.verified, k.verification_status = True, "verified"
    p = await make_purchase(db, user, unified, crypto_symbol="XRP", status="active")
    await db.commit()
    Venue.answers = {await plain(db, unified): UNIFIED}
    out = await uc.check_unified_accounts(db)
    # the shared test database may hold other verified keys; assert on ours and on what this pass produced
    assert out["checked"] >= 2 and out["turned_unified"] == 1 and out["emails"] == 1
    await db.refresh(unified); await db.refresh(classic)
    assert unified.verified is False and unified.verification_status == "failed" and unified.verification_error == kv.UNIFIED_MESSAGE
    assert classic.verified is True and classic.verification_status == "verified"
    assert len(mail.sent) == 1 and mail.sent[0]["to"] == user.email and kv.UNIFIED_MESSAGE in mail.sent[0]["text"]
    assert mail.sent[0]["category"] == "failure"   # not an optional category: it goes out regardless of marketing preferences
    assert len(tg) == 1 and tg[0][1] == f"unified_account:{unified.id}" and user.email not in tg[0][0]
    # the purchases list carries the message for the banner
    from api.auth import create_access_token
    r = await client.get("/api/v2/purchases", headers={"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"})
    row = next(x for x in r.json() if x["id"] == str(p.id))
    assert row["exchange_key_error"] == kv.UNIFIED_MESSAGE
    # next day: the flagged key is no longer verified, so it is not checked again: no second email or alert
    Venue.calls.clear()
    out2 = await uc.check_unified_accounts(db)
    assert out2["turned_unified"] == 0 and out2["emails"] == 0
    assert (await plain(db, classic)) in Venue.calls and (await plain(db, unified)) not in Venue.calls
    assert len(mail.sent) == 1 and len(tg) == 1


async def test_other_venue_errors_count_as_unreadable_and_change_nothing(db, user, mail, tg):
    key = await make_key(db, user, exchange="bitget")
    key.verified, key.verification_status = True, "verified"
    await db.commit()
    Venue.answers = {await plain(db, key): ExchangeClientError("Bitget error 40018: Invalid IP", "40018")}
    out = await uc.check_unified_accounts(db)
    assert out["unreadable"] >= 1 and out["turned_unified"] == 0 and out["emails"] == 0
    await db.refresh(key)
    assert key.verified is True and key.verification_status == "verified" and mail.sent == [] and tg == []


async def test_the_daily_job_is_registered_and_skips_a_taken_tick(monkeypatch):
    from api.tasks import scheduler as sch
    from api.services.redis_client import get_redis
    sched = sch.build_scheduler()
    job = sched.get_job("unified_account_check")
    assert job is not None and str(job.trigger).startswith("cron")
    assert len(sched.get_jobs()) == 7
    r = get_redis(); await r.delete("autobuy:job:unified_account_check:tick")
    assert await sch._claim_tick("unified_account_check", ttl=3600) is True
    def boom(*a, **k):
        raise AssertionError("database engine built on a skipped tick")
    monkeypatch.setattr(sch, "create_async_engine", boom)
    assert await sch._check_unified_once() == {"skipped": "tick"}
    await r.delete("autobuy:job:unified_account_check:tick")
