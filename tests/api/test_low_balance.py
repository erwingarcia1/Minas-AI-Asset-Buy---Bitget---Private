"""Pre-buy low-balance warning: a read-only job that warns once per run about a day
and about two hours before an underfunded scheduled buy, clears when topped up,
reports unreadable balances to admins only, and never touches an order path."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Purchase, PurchaseTransaction, User
from api.services import low_balance as lb
from api.services import telegram
from api.services.notifications import Preferences
from tests.api.test_email_service import CapturingEmail
from tests.conftest_autobuy import auth, bitget_key, db, make_key, make_purchase, user  # noqa: F401

NOW = datetime(2026, 10, 3, 0, 12, tzinfo=timezone.utc)   # XRP-style run time on 2026-10-04 00:12 UTC is 24 h away
RUN = NOW + timedelta(hours=24)


@pytest.fixture
def mail(monkeypatch):
    capture = CapturingEmail()
    monkeypatch.setattr(lb, "email_service", capture)
    return capture


@pytest.fixture
def tg(monkeypatch):
    sent = []
    async def fake_send(text, key=None):
        sent.append((text, key)); return True
    monkeypatch.setattr(telegram, "send", fake_send)
    return sent


@pytest.fixture
def no_order_path(monkeypatch):
    """Any attempt to build an exchange client (the only way to an order endpoint) blows up."""
    from api.services import exchange_gateway
    def boom(account):
        raise AssertionError("the low-balance job must never build an exchange client through the gateway")
    monkeypatch.setattr(exchange_gateway, "_client_for", boom)


def reader(balance):
    calls = []
    def read(account):
        calls.append(account.id); return balance
    read.calls = calls
    return read


async def reload(db, pid):
    return (await db.execute(select(Purchase).where(Purchase.id == pid).execution_options(populate_existing=True))).scalar_one()


def test_windows_and_margin():
    assert lb.in_window(RUN, NOW) == 1                                   # exactly a day before
    assert lb.in_window(RUN, NOW - timedelta(minutes=50)) == 1          # 24 h 50 min before, inside 23–25 h
    assert lb.in_window(RUN, NOW + timedelta(hours=22)) == 2            # 2 h before
    assert lb.in_window(RUN, NOW + timedelta(hours=23, minutes=30)) is None   # 30 min before: too late, the buy is about to run
    assert lb.in_window(RUN, NOW + timedelta(hours=12)) is None         # between the windows
    assert lb.in_window(RUN, NOW + timedelta(hours=25)) is None         # already past
    assert lb.needed_usdt(type("P", (), {"amount_usd": 10})()) == pytest.approx(10.1)


async def test_short_balance_warns_once_per_run_by_email_banner_and_telegram(db, user, bitget_key, mail, tg, no_order_path):
    p = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", amount_usd=10.0, frequency="weekly", next_execution=RUN)
    read = reader(3.5)
    out = await lb.check_low_balances(db, now=NOW, balance_reader=read)
    assert out["checked"] == 1 and out["alerted"] == 1 and out["unreadable"] == 0
    assert read.calls == [bitget_key.id]
    # email: subject, UTC time, amounts with 2 decimals, how to top up, unsubscribe link
    assert len(mail.sent) == 1
    m = mail.sent[0]
    assert m["to"] == user.email and m["subject"] == "Top up before your next AutoBuy purchase" and m["template"] == "low_balance"
    assert "Saturday, Oct 3 at 8:12 PM ET (00:12 UTC)" in m["text"] and "Times are Eastern Time with UTC in brackets" in m["text"]
    assert "10.00 USDT of XRP" in m["text"] and "Needs about:    10.10 USDT" in m["text"]
    assert "Available now:  3.50 USDT" in m["text"] and "Shortfall:      6.60 USDT" in m["text"]
    assert "spot" in m["text"].lower() and "/settings/notifications" in m["text"] and "/purchases" in m["text"]
    # telegram: one masked line, no raw email
    assert len(tg) == 1 and "Low balance before a buy" in tg[0][0] and user.email not in tg[0][0] and "XRP" in tg[0][0]
    # bookkeeping + API banner flag
    p = await reload(db, p.id)
    assert p.low_balance_alerted_for == RUN and p.low_balance_alerted_at == NOW
    assert p.status == "active" and p.next_execution == RUN and p.failure_count == 0, "schedule untouched"
    import httpx
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        row = (await c.get(f"/api/v2/purchases/{p.id}", headers=auth(user))).json()
        assert row["low_balance_for_next_run"] is True and row["low_balance_alerted_at"] is not None
        stats = (await c.get("/api/dashboard/stats", headers=auth(user))).json()
        assert [n["crypto_symbol"] for n in stats["low_balance"]] == ["XRP"]
    # the same window half an hour later: still short, nothing new
    out = await lb.check_low_balances(db, now=NOW + timedelta(minutes=30), balance_reader=read)
    assert out["already_alerted"] == 1 and out["alerted"] == 0 and len(mail.sent) == 1 and len(tg) == 1
    assert not (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalars().all(), "no attempt rows"


async def test_enough_balance_sends_nothing(db, user, bitget_key, mail, tg, no_order_path):
    p = await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN)
    out = await lb.check_low_balances(db, now=NOW, balance_reader=reader(10.10))   # exactly amount + 1%
    assert out["checked"] == 1 and out["sufficient"] == 1 and out["alerted"] == 0
    assert mail.sent == [] and tg == []
    p = await reload(db, p.id)
    assert p.low_balance_alerted_for is None and p.low_balance_alerted_at is None


async def test_topped_up_later_clears_the_banner(db, user, bitget_key, mail, tg, no_order_path):
    p = await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN)
    await lb.check_low_balances(db, now=NOW, balance_reader=reader(1.0))             # window one: short
    assert lb.low_balance_outstanding(await reload(db, p.id))
    out = await lb.check_low_balances(db, now=NOW + timedelta(hours=22), balance_reader=reader(50.0))   # window two: topped up
    assert out["cleared"] == 1 and out["alerted"] == 0
    p = await reload(db, p.id)
    assert p.low_balance_alerted_for is None and p.low_balance_alerted_at is None and not lb.low_balance_outstanding(p)
    assert len(mail.sent) == 1, "the earlier warning is the only email"


async def test_window_two_warns_when_window_one_never_ran(db, user, bitget_key, mail, tg, no_order_path):
    p = await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN)
    out = await lb.check_low_balances(db, now=NOW + timedelta(hours=22), balance_reader=reader(2.0))
    assert out["alerted"] == 1 and len(mail.sent) == 1 and lb.low_balance_outstanding(await reload(db, p.id))


async def test_unreadable_balance_alerts_admins_only(db, user, bitget_key, mail, tg, no_order_path):
    p = await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN)
    def read(account):
        raise RuntimeError("Bitget 40037: key not found")
    out = await lb.check_low_balances(db, now=NOW, balance_reader=read)
    assert out["unreadable"] == 1 and out["alerted"] == 0
    assert mail.sent == [], "no customer email on an unreadable balance"
    assert len(tg) == 1 and "could not read a balance" in tg[0][0] and "No customer email sent" in tg[0][0] and user.email not in tg[0][0]
    p = await reload(db, p.id)
    assert p.low_balance_alerted_for is None and p.status == "active"


async def test_preference_off_skips_the_email_but_keeps_banner_and_admin_line(db, user, bitget_key, mail, tg, no_order_path):
    prefs = Preferences.for_user(user); prefs.email_low_balance = False
    user.notification_preferences = prefs.to_json(); await db.commit()
    p = await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN)
    out = await lb.check_low_balances(db, now=NOW, balance_reader=reader(1.0))
    assert out["alerted"] == 1 and mail.sent == [] and len(tg) == 1 and "no email (preference off" in tg[0][0]
    assert lb.low_balance_outstanding(await reload(db, p.id))


async def test_skips_paused_cancelled_completed_past_due_and_out_of_window(db, user, bitget_key, mail, tg, no_order_path):
    await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN, status="paused")
    await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN, status="cancelled")
    await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=None, status="completed", frequency="one_time")   # one-time already ran
    await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=NOW - timedelta(hours=1))                           # due in the past
    await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=NOW + timedelta(hours=10))                          # between the windows
    read = reader(0.0)
    out = await lb.check_low_balances(db, now=NOW, balance_reader=read)
    assert out["checked"] == 0 and read.calls == [] and mail.sent == [] and tg == []


async def test_skips_ineligible_owners_and_unverified_keys_and_reads_each_account_once(db, user, bitget_key, mail, tg, no_order_path):
    await make_purchase(db, user, bitget_key, amount_usd=10.0, next_execution=RUN)
    await make_purchase(db, user, bitget_key, amount_usd=20.0, next_execution=RUN + timedelta(minutes=5))
    read = reader(15.0)   # enough for the 10, short for the 20
    out = await lb.check_low_balances(db, now=NOW, balance_reader=read)
    assert out["checked"] == 2 and read.calls == [bitget_key.id], "one balance read per account per pass"
    # 15 USDT against 10 + 20 (+1%): the ACCOUNT is short, so both purchases are flagged in one email
    assert out["alerted"] == 2 and out["sufficient"] == 0 and out["emails"] == 1 and len(mail.sent) == 1
    assert "20.00 USDT of BTC" in mail.sent[0]["text"] and "10.00 USDT of BTC" in mail.sent[0]["text"] and "30.30 USDT in total" in mail.sent[0]["text"]
    # unverified key: not a balance problem
    bitget_key.verified = False; await db.commit()
    read = reader(0.0)
    out = await lb.check_low_balances(db, now=NOW + timedelta(minutes=30), balance_reader=read)
    assert out["checked"] == 0 and read.calls == []
    bitget_key.verified = True; user.suspended = True; await db.commit()
    out = await lb.check_low_balances(db, now=NOW + timedelta(minutes=30), balance_reader=read)
    assert out["checked"] == 0 and read.calls == []
    user.suspended = False; await db.commit()


def test_job_is_registered_every_30_minutes_and_never_imports_an_order_function():
    from api.tasks import scheduler as sched_mod
    sched = sched_mod.build_scheduler()
    job = sched.get_job("low_balance_check")
    assert job is not None and job.func is sched_mod.check_low_balances_job
    assert job.trigger.interval == timedelta(minutes=30)
    import inspect
    src = inspect.getsource(lb)
    for forbidden in ("place_buy", "execute_order", "withdraw(", "execute_due_purchases", "_withdraw_after_buy"):
        assert forbidden not in src, forbidden


async def test_preferences_api_exposes_the_new_category(db, user):
    import httpx
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/notifications/preferences", headers=auth(user))
        assert r.status_code == 200 and r.json()["email_low_balance"] is True, "on by default"
        r = await c.put("/api/notifications/preferences", headers=auth(user), json={"email_low_balance": False})
        assert r.status_code == 200 and r.json()["email_low_balance"] is False
        fresh = (await db.execute(select(User).where(User.id == user.id).execution_options(populate_existing=True))).scalar_one()
        assert Preferences.for_user(fresh).wants_email("email_low_balance") is False


async def test_two_purchases_on_one_account_are_summed(db, user, bitget_key, mail, tg, no_order_path):
    """Two 10 USDT purchases on the same exchange account, 15 USDT available: each alone would pass,
    together they need 20.20. Both are flagged, one email lists both, one Telegram line."""
    a = await make_purchase(db, user, bitget_key, crypto_symbol="XRP", amount_usd=10.0, next_execution=RUN)
    b = await make_purchase(db, user, bitget_key, crypto_symbol="BTC", amount_usd=10.0, next_execution=RUN + timedelta(minutes=30))
    read = reader(15.0)
    out = await lb.check_low_balances(db, now=NOW, balance_reader=read)
    assert read.calls == [bitget_key.id] and out["checked"] == 2 and out["alerted"] == 2 and out["emails"] == 1
    assert lb.low_balance_outstanding(await reload(db, a.id)) and lb.low_balance_outstanding(await reload(db, b.id))
    m = mail.sent[0]["text"]
    assert "Your next 2 scheduled buys share one Bitget balance" in m
    assert "10.00 USDT of XRP" in m and "10.00 USDT of BTC" in m and "Needs about:    20.20 USDT in total" in m
    assert "Available now:  15.00 USDT" in m and "Shortfall:      5.20 USDT" in m
    assert len(tg) == 1 and "needs ~20.20 USDT in total, has 15.00 USDT (short 5.20)" in tg[0][0]
    # topped up to 25: enough for both; both banners clear, nothing sent
    out = await lb.check_low_balances(db, now=NOW + timedelta(minutes=30), balance_reader=reader(25.0))
    assert out["cleared"] == 2 and out["alerted"] == 0 and len(mail.sent) == 1 and len(tg) == 1
    assert not lb.low_balance_outstanding(await reload(db, a.id)) and not lb.low_balance_outstanding(await reload(db, b.id))


def test_run_time_is_eastern_first_with_utc_in_brackets():
    from datetime import datetime, timezone
    assert lb.fmt_run_time(datetime(2026, 10, 12, 0, 16, 57, tzinfo=timezone.utc)) == "Sunday, Oct 11 at 8:16 PM ET (00:16 UTC)"
    assert lb.fmt_run_time(datetime(2026, 12, 1, 13, 5, tzinfo=timezone.utc)) == "Tuesday, Dec 1 at 8:05 AM ET (13:05 UTC)"   # EST after the clock change
