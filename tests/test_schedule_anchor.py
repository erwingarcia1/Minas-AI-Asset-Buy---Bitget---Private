"""Schedule anchor (migration 027): recurring runs are computed from a fixed anchor,
never from the moment a buy happened, so the scheduler's tick offset cannot drift
the weekly time, a short month cannot stick the day, a resume buys once and snaps
back to the grid, and retries never move anything."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from api.models import Purchase, PurchaseTransaction
from api.services.purchase_executor import next_on_grid
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import auth, bitget_key, db, make_key, make_purchase, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_withdrawal_ops import reload

DUE = datetime(2026, 10, 5, 0, 12, 8, tzinfo=timezone.utc)   # the live XRP schedule before it drifted
ET = ZoneInfo("America/New_York")


def api():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def buys(db, pid):
    return [t for t in (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == pid))).scalars().all() if t.tx_type == "buy"]


def test_grid_maths():
    assert next_on_grid(DUE, "weekly", DUE + timedelta(minutes=4, seconds=49)) == DUE + timedelta(days=7)
    assert next_on_grid(DUE, "daily", DUE + timedelta(hours=23)) == DUE + timedelta(days=1)
    assert next_on_grid(DUE, "weekly", DUE - timedelta(days=1)) == DUE, "before the anchor: the anchor itself"
    assert next_on_grid(DUE, "one_time", DUE) is None
    jan31 = datetime(2026, 1, 31, 10, 0, tzinfo=timezone.utc)
    feb = next_on_grid(jan31, "monthly", jan31 + timedelta(seconds=1)); mar = next_on_grid(jan31, "monthly", feb + timedelta(seconds=1)); apr = next_on_grid(jan31, "monthly", mar + timedelta(seconds=1))
    assert (feb.date().isoformat(), mar.date().isoformat(), apr.date().isoformat()) == ("2026-02-28", "2026-03-31", "2026-04-30")
    with pytest.raises(ValueError):
        next_on_grid(DUE, "hourly", DUE)


async def test_a_late_run_keeps_the_weekly_time(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", frequency="weekly", wallet_address=None, blockchain_network=None,
                            next_execution=DUE, schedule_anchor=DUE)
    now = DUE + timedelta(minutes=4, seconds=49)   # picked up by the next 5-minute tick
    await ps.execute_due_purchases(db, now=now, gateway=FakeGateway(), dry_run=False)
    p = await reload(db, p.id)
    assert len(await buys(db, p.id)) == 1 and p.status == "active"
    assert p.next_execution == DUE + timedelta(days=7), "seven days after the DUE time, not after the late run"
    assert p.schedule_anchor == DUE, "execution never touches the anchor"


async def test_monthly_anchor_recovers_after_a_short_month(db, user, bitget_key):
    jan31 = datetime(2026, 1, 31, 10, 0, tzinfo=timezone.utc)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", frequency="monthly", wallet_address=None, blockchain_network=None,
                            next_execution=jan31, schedule_anchor=jan31)
    for run_at, expect_next in ((jan31 + timedelta(minutes=3), "2026-02-28"), (datetime(2026, 2, 28, 10, 2, tzinfo=timezone.utc), "2026-03-31"),
                                (datetime(2026, 3, 31, 10, 4, tzinfo=timezone.utc), "2026-04-30")):
        await ps.execute_due_purchases(db, now=run_at, gateway=FakeGateway(), dry_run=False)
        p = await reload(db, p.id)
        assert p.next_execution.date().isoformat() == expect_next and p.next_execution.strftime("%H:%M") == "10:00", expect_next
    assert len(await buys(db, p.id)) == 3 and p.schedule_anchor == jan31


async def test_paused_then_resumed_buys_once_and_snaps_back_to_the_grid(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", frequency="weekly", wallet_address=None, blockchain_network=None,
                            next_execution=DUE, schedule_anchor=DUE, status="paused")
    resume_at = DUE + timedelta(days=21, hours=5)   # three missed weeks
    async with api() as c:
        r = await c.put(f"/api/v2/purchases/{p.id}", json={"status": "active"}, headers=auth(user))
        assert r.status_code == 200
    p = await reload(db, p.id)
    assert p.status == "active" and p.schedule_anchor == DUE
    # the resumed purchase is due now: one buy, never one per missed week
    await ps.execute_due_purchases(db, now=resume_at, gateway=FakeGateway(), dry_run=False)
    await ps.execute_due_purchases(db, now=resume_at + timedelta(minutes=5), gateway=FakeGateway(), dry_run=False)
    p = await reload(db, p.id)
    assert len(await buys(db, p.id)) == 1
    assert p.next_execution == DUE + timedelta(days=28), "back on the weekly grid, not resume + 7 days"


async def test_far_past_due_catches_up_with_exactly_one_buy(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", frequency="weekly", wallet_address=None, blockchain_network=None,
                            next_execution=DUE, schedule_anchor=DUE)
    now = DUE + timedelta(days=45)
    await ps.execute_due_purchases(db, now=now, gateway=FakeGateway(), dry_run=False)
    await ps.execute_due_purchases(db, now=now + timedelta(minutes=5), gateway=FakeGateway(), dry_run=False)
    p = await reload(db, p.id)
    assert len(await buys(db, p.id)) == 1 and p.next_execution == DUE + timedelta(days=49) and p.next_execution > now


async def test_retries_never_move_the_anchor_or_the_due_time(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", frequency="weekly", wallet_address=None, blockchain_network=None,
                            next_execution=DUE, schedule_anchor=DUE)
    failing = FakeGateway(fail_buy=True)
    for i in (1, 2):
        await ps.execute_due_purchases(db, now=DUE + timedelta(minutes=5 * i), gateway=failing, dry_run=False)
        p = await reload(db, p.id)
        assert p.failure_count == i and p.next_execution == DUE and p.schedule_anchor == DUE, f"attempt {i}: nothing moves"
    await ps.execute_due_purchases(db, now=DUE + timedelta(minutes=15), gateway=failing, dry_run=False)   # cycle exhausted
    p = await reload(db, p.id)
    assert p.status == "active" and p.next_execution == DUE + timedelta(days=7) and p.schedule_anchor == DUE, "the missed cycle's successor on the grid"


async def test_weekly_anchor_crossing_the_clock_change_stays_on_the_utc_grid(db, user, bitget_key):
    anchor = datetime(2026, 10, 25, 0, 16, tzinfo=timezone.utc)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", frequency="weekly", wallet_address=None, blockchain_network=None,
                            next_execution=anchor, schedule_anchor=anchor)
    seen = []
    for week in range(3):
        due = anchor + timedelta(days=7 * week)
        await ps.execute_due_purchases(db, now=due + timedelta(minutes=4), gateway=FakeGateway(), dry_run=False)
        p = await reload(db, p.id)
        seen.append((due.strftime("%H:%M UTC"), due.astimezone(ET).strftime("%-I:%M %p")))
    assert [s[0] for s in seen] == ["00:16 UTC"] * 3, "the grid is UTC"
    assert [s[1] for s in seen] == ["8:16 PM", "8:16 PM", "7:16 PM"], "Eastern display shifts an hour after 1 Nov (EDT -> EST)"
    assert p.next_execution == anchor + timedelta(days=21) and p.next_execution.strftime("%H:%M") == "00:16"


async def test_create_sets_the_anchor_and_a_frequency_edit_resets_it(db, user, bitget_key):
    async with api() as c:
        r = await c.post("/api/v2/purchases", headers=auth(user), json={"crypto_symbol": "BTC", "amount_usd": 20, "frequency": "weekly",
                                                                        "exchange_account_id": str(bitget_key.id), "purchase_type": "dca"})
        assert r.status_code == 201, r.text
        pid = r.json()["purchase_id"]
        p = await reload(db, pid)
        assert p.schedule_anchor == p.next_execution, "anchored on the first due time"
        before = p.schedule_anchor
        r = await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"amount_usd": 25})
        assert r.status_code == 200 and (await reload(db, pid)).schedule_anchor == before, "an amount edit keeps the anchor"
        r = await c.put(f"/api/v2/purchases/{pid}", headers=auth(user), json={"frequency": "daily"})
        assert r.status_code == 200
        p = await reload(db, pid)
        assert p.schedule_anchor > before and p.frequency == "daily"
        assert p.next_execution == p.schedule_anchor + timedelta(days=1), "a new cadence starts one period after the edit"


async def test_a_purchase_created_without_an_anchor_anchors_on_its_due_time(db, user, bitget_key):
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", wallet_address=None, blockchain_network=None, next_execution=DUE)
    assert (await reload(db, p.id)).schedule_anchor == DUE
