"""Unified (v3) rejection codes map to the same outcomes as their Classic equivalents.

The 2026-10-10 incident: a v3 buy raised 25202 (Insufficient balance), the lookup
then raised 25204 (Order does not exist) and the purchase parked as needs_review.
After this fix:

- a DEFINITE refusal (insufficient balance, below minimum, symbol not tradeable,
  no trade permission) fails the attempt outright — same outcome, same customer
  emails as the Classic path's 43012-style failures — never needs_review;
- an UNKNOWN placement error keeps the ask-the-exchange-first path: found ->
  recorded; "not found" -> one re-check after a pause -> still nothing -> failed;
  lookup error -> needs_review;
- the v3 client answers "order does not exist" (25204) as None / not_found,
  exactly like the Classic client's 43001 family.
"""
import pytest

from api.services import purchase_executor as pe
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for


@pytest.fixture
def slack(monkeypatch):
    from api.services import slack_service
    calls = []

    async def fake(text):
        calls.append(text)
        return True
    monkeypatch.setattr(slack_service, "send_slack", fake)
    return calls


@pytest.fixture
def no_wait(monkeypatch):
    """Record the re-check pause instead of sleeping through it."""
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(pe.asyncio, "sleep", fake_sleep)
    return sleeps


def refusal(code, message):
    return ps.ExecutionError(f"{code}: {message}", stage="buy", code=code)


# ------------------------------------------------- definite refusals -> failed, no lookup

async def test_25202_fails_without_review_and_without_lookup(db, user, bitget_key, slack, no_wait):
    """The incident case: insufficient balance on a Unified key is a plain failure,
    identical in outcome to Classic 43012 — and the lookup that used to raise
    25204 is never even made."""
    gw = FakeGateway(fail_buy_error=refusal("25202", "Insufficient balance"),
                     lookup_error=ps.ExecutionError("25204: Order does not exist", stage="lookup", code="25204"))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", amount_usd=2,
                            purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "failed"
    assert buy.error_message == "25202: Insufficient balance"
    assert gw.placed == [] and gw.lookups == [], "a definite refusal is never looked up"
    await db.refresh(p)
    assert p.status == "active" and p.failure_count == 1, "retried on the next tick, like Classic"
    assert not any("NEEDS REVIEW" in s for s in slack), "never parked for review"
    assert no_wait == []


@pytest.mark.parametrize("code,message", [
    ("25202", "Insufficient balance"),                                        # Classic twin: 43012
    ("25207", "PEPEUSDT trading quantity cannot be less than 1 units"),       # below minimum size
    ("25611", "Order quantity must be greater than the minimum order amount"),
    ("25100", "Trading pair PEPEUSDT does not exist"),                        # symbol not tradeable
    ("25101", "Trading pair not open for trading"),
    ("25102", "Trading pair temporarily closed for maintenance"),
    ("25013", "PEPE not support API trade"),                                  # key lacks permission
    ("25620", "permission denied"),
    ("SYMBOL", "PEPEUSDT is not trading (status=offline)"),                   # our own pre-send checks
    ("MIN_NOTIONAL", "0.5 USDT is below the 1 USDT minimum for PEPEUSDT"),
])
async def test_every_definite_refusal_is_a_plain_failure(db, user, bitget_key, code, message, no_wait):
    gw = FakeGateway(fail_buy_error=refusal(code, message))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", amount_usd=2,
                            purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "failed" and buy.error_message == f"{code}: {message}"
    assert gw.lookups == []
    await db.refresh(p)
    assert p.status == "active", "never needs_review"


async def test_stopped_one_time_purchase_emails_the_same_text_as_classic(db, user, bitget_key, monkeypatch, no_wait):
    """A one-time purchase that keeps failing on 25202 exhausts its attempts and is
    stopped, with the exact customer email the Classic path sends on 43012."""
    from api.services import email_service as es_mod
    from tests.api.test_email_service import CapturingEmail
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    gw = FakeGateway(fail_buy_error=refusal("25202", "Insufficient balance"))
    p = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", amount_usd=2,
                            purchase_type="dca", frequency="one_time", next_execution=NOW)
    for i in range(3):   # MAX_ATTEMPTS ticks, 5 minutes apart
        await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    await db.refresh(p)
    assert p.status == "failed" and p.next_execution is None
    mails = [m for m in capture.sent if m["template"] == "purchase"]
    assert len(mails) == 1, "one email, when the purchase is stopped — not one per attempt"
    assert mails[0]["subject"] == "We've paused your PEPE purchase. No money was spent"
    assert "didn't have enough USDT" in mails[0]["text"]
    assert "25202" not in mails[0]["text"], "venue codes never reach the customer"
    assert "Nothing was bought and no money left your account" in mails[0]["text"]
    assert mails[0]["text"].endswith("Need help? Reply to this email or ask in the AutoBuy Telegram group.")


# ------------------------------------------------- unknown errors keep the lookup path

async def test_unknown_error_with_order_found_is_recorded(db, user, bitget_key, no_wait):
    gw = FakeGateway(place_raises_after_sending=True)   # raises NETWORK after the order reached the venue
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "success" and len(gw.placed) == 1 and len(gw.lookups) == 1
    assert no_wait == [], "a found order needs no re-check"


async def test_unknown_error_not_found_twice_is_failed_after_one_recheck(db, user, bitget_key, slack, no_wait):
    gw = FakeGateway(fail_buy_error=refusal("99999", "some new rejection"),
                     lookup_states=["not_found", "not_found"])
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "failed" and "99999" in buy.error_message
    assert len(gw.lookups) == 2, "looked up, waited, looked up once more"
    assert no_wait == [pe.LOOKUP_RECHECK_SECONDS]
    await db.refresh(p)
    assert p.status == "active" and not any("NEEDS REVIEW" in s for s in slack)


async def test_unknown_error_found_on_the_second_lookup_is_recorded(db, user, bitget_key, no_wait):
    """The reason for the re-check: v3 order-info can lag a just-placed order."""
    gw = FakeGateway(fail_buy_error=refusal("TIMEOUT", "read timed out"),
                     lookup_states=["not_found", "filled"])
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "success", "the late-appearing fill is recorded, never re-bought"
    assert len(gw.lookups) == 2 and no_wait == [pe.LOOKUP_RECHECK_SECONDS]
    assert gw.placed == [], "and no second order was ever sent"


async def test_lookup_error_40404_is_needs_review(db, user, bitget_key, slack, no_wait):
    gw = FakeGateway(fail_buy_error=refusal("99999", "some new rejection"),
                     lookup_error=ps.ExecutionError("40404: Request URL NOT FOUND", stage="lookup", code="40404"))
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "needs_review"
    assert "could not be looked up: 40404" in buy.error_message
    await db.refresh(p)
    assert p.status == "needs_review" and any("NEEDS REVIEW" in s for s in slack)


async def test_recheck_lookup_error_is_needs_review(db, user, bitget_key, slack, no_wait):
    """First lookup says not found, the re-check errors: park for a human, do not guess."""
    gw = FakeGateway(fail_buy_error=refusal("99999", "some new rejection"),
                     lookup_states=["not_found"],
                     lookup_error=None)
    # after lookup_states is exhausted the fake falls through; script the second call to raise
    original = gw.lookup_order

    def flaky(account, purchase, client_oid):
        if len(gw.lookups) >= 1:
            gw.lookups.append(client_oid)
            raise ps.ExecutionError("NETWORK: exchange unreachable", stage="lookup", code="NETWORK")
        return original(account, purchase, client_oid)
    gw.lookup_order = flaky
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "needs_review" and "could not be looked up" in buy.error_message


# ------------------------------------------------- Classic paths unchanged

async def test_classic_43012_outcome_is_unchanged(db, user, bitget_key, slack, no_wait):
    """Classic insufficient balance (43012) is NOT in the definite-refusal table:
    it keeps the ask-the-exchange-first path and still ends failed, never review."""
    gw = FakeGateway(fail_buy_error=refusal("43012", "Insufficient balance"))
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    (buy,) = await tx_for(db, p)
    assert buy.status == "failed" and buy.error_message == "43012: Insufficient balance"
    assert len(gw.lookups) == 2, "asked the exchange (and re-checked once) before failing"
    await db.refresh(p)
    assert p.status == "active" and p.failure_count == 1
    assert not any("NEEDS REVIEW" in s for s in slack)
