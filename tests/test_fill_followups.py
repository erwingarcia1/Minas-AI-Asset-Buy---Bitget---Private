"""Warning band on fills, missed-cycle retry policy, and the admin path out of
needs_review. Everything here runs against the fake gateway."""
import logging
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from api.config import settings
from api.models import PurchaseTransaction, User
from api.services import fill_watch, history
from api.tasks import purchase_scheduler as ps
from tests.api.conftest import auth_headers
from tests.conftest_autobuy import binance_key, bitget_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import BuyFails, FakeGateway
from tests.test_fill_guard import reload
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


@pytest.fixture(autouse=True)
async def clean_fill_watch():
    from api.services.redis_client import get_redis
    r = get_redis()
    keys = [k async for k in r.scan_iter("autobuy:fill:deviations:*")]
    if keys:
        await r.delete(*keys)
    yield
    keys = [k async for k in r.scan_iter("autobuy:fill:deviations:*")]
    if keys:
        await r.delete(*keys)


@pytest.fixture
async def admin(db):
    u = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh)
        await db.commit()


async def fresh_rows(db, purchase_id):
    """Rows re-read from the database (the API commits through its own session)."""
    return (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == purchase_id)
                             .execution_options(populate_existing=True)
                             .order_by(PurchaseTransaction.created_at))).scalars().all()


def api():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- 1. warning band

async def test_fill_inside_the_warning_band_is_recorded_logged_and_counted(db, user, bitget_key, slack, caplog, monkeypatch, admin):
    monkeypatch.setattr(settings, "fill_tolerance_pct", 10)
    monkeypatch.setattr(settings, "fill_warn_pct", 3)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=50, next_execution=NOW)
    gw = FakeGateway(reference_price=50_000.0, fill_factor=0.95)   # 5% short: warn, do not park
    with caplog.at_level(logging.WARNING, logger="autobuy.fill_watch"):
        counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert counts["executed"] == 1 and slack == [], "the customer is not blocked"
    (tx,) = await tx_for(db, p)
    assert tx.status == "success" and tx.amount_executed == pytest.approx(0.001 * 0.95)
    line = next(r.getMessage() for r in caplog.records if "fill deviation" in r.getMessage())
    assert "5.00%" in line and "expected 0.00100000 got 0.00095000" in line and "50.00 USDT" in line
    assert await fill_watch.count_24h() == 1
    recent = await fill_watch.recent()
    assert recent[0]["purchase_id"] == str(p.id) and recent[0]["deviation_pct"] == 5.0
    # a normal fill (0.1% fee-sized) is not counted
    await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=50, next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(reference_price=50_000.0, fill_factor=0.999), dry_run=True)
    assert await fill_watch.count_24h() == 1
    # the alert: ok below 3, warning at 3, critical at 10
    async with api() as c:
        H = auth_headers(admin)
        assert {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=H)).json()}["fill_deviations"]["severity"] == "ok"
        for _ in range(2):
            await fill_watch.record(p.id, user.id, "BTC", 4.0, 0.001, 0.00096, 50, 50, 50_000.0)
        alerts = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=H)).json()}
        assert alerts["fill_deviations"]["severity"] == "warning" and alerts["fill_deviations"]["value"] == "3"
        devs = (await c.get("/api/admin/fills/deviations", headers=H)).json()
        assert len(devs) == 3 and devs[-1]["deviation_pct"] == 5.0
        for _ in range(7):
            await fill_watch.record(p.id, user.id, "BTC", 4.0, 0.001, 0.00096, 50, 50, 50_000.0)
        assert {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=H)).json()}["fill_deviations"]["severity"] == "critical"


# --------------------------------------------------------------------------- 3. cycles

async def test_a_recurring_purchase_misses_the_cycle_after_three_attempts_and_moves_on(db, user, bitget_key, slack, monkeypatch, admin):
    from api.services import email_service as es_mod
    from tests.api.test_email_service import CapturingEmail
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, frequency="daily", next_execution=NOW)
    for i in range(3):
        counts = await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5 * i), gateway=BuyFails(), dry_run=True)
        assert counts["failed"] == 1
    p = await reload(db, p)
    assert p.status == "active", "not stalled"
    assert p.failure_count == 3 and p.next_execution == NOW + timedelta(days=1), "moved on to the next normal interval"
    assert "missed this cycle (1 of 3 in a row)" in p.last_error
    # not due again before then
    assert (await ps.execute_due_purchases(db, now=NOW + timedelta(hours=3), gateway=BuyFails(), dry_run=True)) == {"executed": 0, "failed": 0, "skipped": 0}
    # the customer heard ONCE, when the cycle was exhausted: attempts 1 and 2 send nothing
    mails = [m for m in capture.sent if "Purchase missed" in m["subject"] or "Purchase failed" in m["subject"]]
    assert len(mails) == 1 and "Purchase missed: BTC" in mails[0]["subject"]
    assert "tried 3 times" in mails[0]["text"] and "missed for this cycle" in mails[0]["text"]
    assert "next attempt is at" in mails[0]["text"] and "no money was spent" in mails[0]["text"]
    # Slack: one line per run (three runs), never a per-attempt line per purchase
    run_lines = [s for s in slack if "purchase(s) failed a buy attempt" in s]
    assert len(run_lines) == 3 and all("1 purchase(s)" in s for s in run_lines)
    assert not any(s.startswith(":x: AutoBuy buy failed") for s in slack)
    async with api() as c:
        alerts = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=auth_headers(admin))).json()}
    assert alerts["missed_cycles"]["severity"] == "warning" and alerts["stopped_purchases"]["severity"] == "ok"
    # a success later clears the streak
    await ps.execute_due_purchases(db, now=NOW + timedelta(days=2), gateway=FakeGateway(), dry_run=True)
    p = await reload(db, p)
    assert p.status == "active" and p.failure_count == 0 and p.last_error is None


async def test_three_missed_cycles_in_a_row_stop_the_purchase_and_alert(db, user, bitget_key, slack, admin, monkeypatch):
    from api.services import email_service as es_mod
    from tests.api.test_email_service import CapturingEmail
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, frequency="daily", next_execution=NOW)
    t = NOW
    for attempt in range(9):
        p = await reload(db, p)
        t = max(t, p.next_execution) if p.next_execution else t
        await ps.execute_due_purchases(db, now=t, gateway=BuyFails(), dry_run=True)
        t += timedelta(minutes=5)
    p = await reload(db, p)
    assert p.status == "failed" and p.next_execution is None and p.failure_count == 9
    assert p.last_error.startswith("PURCHASE STOPPED after 9 failed attempts (3 cycles missed in a row)")
    assert any("PURCHASE STOPPED" in s for s in slack)
    mails = capture.sent
    stopped = [m for m in mails if "has been stopped" in m["subject"]]
    assert len(stopped) == 1 and "has now been STOPPED" in stopped[0]["text"] and "no money was spent" in stopped[0]["text"]
    assert len([m for m in mails if "Purchase missed" in m["subject"]]) == 2, "one email per missed cycle before the stop"
    async with api() as c:
        alerts = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=auth_headers(admin))).json()}
    assert alerts["stopped_purchases"]["severity"] == "critical" and alerts["stopped_purchases"]["value"] == "1"
    # admin can re-arm a stopped (failed) purchase; it is not needs_review
    async with api() as c:
        assert (await c.post(f"/api/admin/purchases/{p.id}/force-retry", headers=auth_headers(admin))).status_code == 204


async def test_a_one_time_purchase_stops_after_its_three_attempts(db, user, bitget_key, slack, monkeypatch):
    from api.services import email_service as es_mod
    from tests.api.test_email_service import CapturingEmail
    capture = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", capture)
    p = await make_purchase(db, user, bitget_key, purchase_type="dca", amount_usd=15, frequency="one_time", next_execution=NOW)
    for i in range(3):
        await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5 * i), gateway=BuyFails(), dry_run=True)
    p = await reload(db, p)
    assert p.status == "failed" and p.next_execution is None and p.last_error.startswith("PURCHASE STOPPED after 3 failed attempts:")
    assert any("PURCHASE STOPPED" in s for s in slack)
    # same day: the one-time purchase is stopped within ~15 minutes and that is the one email it gets
    assert [m["subject"] for m in capture.sent] == ["[DRY RUN] ⛔ Your BTC purchase has been stopped"]


def test_attempt_in_cycle_math():
    assert [ps.attempt_in_cycle(n) for n in (1, 2, 3, 4, 5, 6, 7)] == [1, 2, 3, 1, 2, 3, 1]


# --------------------------------------------------------------------------- 4. the way out of needs_review

async def park(db, user, key, **kw):
    p = await make_purchase(db, user, key, purchase_type=kw.pop("purchase_type", "dca"), amount_usd=50, next_execution=NOW, **kw)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(fill_factor=0.0), dry_run=False)
    p = await reload(db, p)
    assert p.status == "needs_review"
    return p


async def test_admin_lists_reviews_and_records_a_confirmed_fill(db, user, bitget_key, admin, slack, monkeypatch):
    from api.routes.admin import purchases as admin_routes   # the module whose _gateway the review routes call
    p = await park(db, user, bitget_key, purchase_type="autobuy")
    H = auth_headers(admin)
    gw = FakeGateway(lookup_state="filled")          # the exchange now shows a proper fill for the client id
    gateway_calls = []
    monkeypatch.setattr(admin_routes, "_gateway", lambda: gateway_calls.append(1) or gw)
    async with api() as c:
        listed = (await c.get("/api/admin/purchases", headers=H)).json()
        row = next(r for r in listed["items"] if r["id"] == str(p.id))
        assert row["status"] == "needs_review" and row["email"] == user.email and len(row["parked_attempts"]) == 1
        assert "amount 0" in row["parked_attempts"][0]["error_message"]
        # force-retry is refused: it would buy again
        r = await c.post(f"/api/admin/purchases/{p.id}/force-retry", headers=H)
        assert r.status_code == 409 and "review" in r.json()["detail"]
        review = (await c.get(f"/api/admin/purchases/{p.id}/review", headers=H)).json()
        (look,) = review["lookups"]
        assert look["state"] == "filled" and look["verdict"] == "ok" and look["fill"]["amount_crypto"] == pytest.approx(0.001)
        assert gw.lookups == [row["parked_attempts"][0]["client_oid"]]
        bad = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H, json={"action": "record_fill", "note": "ok"})
        assert bad.status_code == 422, "a note is required"
        r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H,
                         json={"action": "record_fill", "note": "Bitget order history shows the order filled 0.001 BTC"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active" and r.json()["parked_attempts"] == []
    rows = await fresh_rows(db, p.id)
    buy = next(t for t in rows if t.tx_type == "buy")
    assert buy.status == "success" and buy.amount_executed == pytest.approx(0.001) and "[admin: fill recorded" in buy.error_message
    assert gw.placed == [], "never re-bought"
    assert gateway_calls, "the review/resolve routes used the patched gateway, not the real one"
    assert any(t.tx_type == "withdrawal" and t.status == "success" for t in rows), "autobuy: the withdrawal was sent after recording"
    p = await reload(db, p)
    assert p.status == "active" and p.failure_count == 0 and p.next_execution is not None


async def test_record_fill_is_refused_when_the_exchange_does_not_show_a_sane_fill(db, user, bitget_key, admin, monkeypatch):
    from api.routes.admin import purchases as admin_routes   # the module whose _gateway the review routes call
    p = await park(db, user, bitget_key)
    H = auth_headers(admin)
    note = {"note": "checked the order on the exchange"}
    gateways = []
    def patched(gw):
        def factory():
            gateways.append(gw)
            return gw
        monkeypatch.setattr(admin_routes, "_gateway", factory)
    async with api() as c:
        patched(FakeGateway(lookup_state="open"))
        r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H, json={"action": "record_fill", **note})
        assert r.status_code == 409 and "reports the order as open" in r.json()["detail"]
        patched(FakeGateway(lookup_state="filled", fill_factor=0.0))
        r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H, json={"action": "record_fill", **note})
        assert r.status_code == 409 and "does not pass the guard" in r.json()["detail"]
        patched(FakeGateway(lookup_raises=True))
        review = (await c.get(f"/api/admin/purchases/{p.id}/review", headers=H)).json()
        assert review["lookups"][0]["state"] == "error" and "unreachable" in review["lookups"][0]["error"]
        r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H, json={"action": "mark_not_sent", **note})
        assert r.status_code == 409, "cannot mark not sent when the exchange could not be asked"
    assert len(gateways) >= 4 and all(g.lookups for g in gateways), "every patched gateway was asked; the real one never was"
    p = await reload(db, p)
    assert p.status == "needs_review", "nothing changed"


async def test_mark_not_sent_makes_the_purchase_due_again_and_cancel_stops_it(db, user, bitget_key, admin, monkeypatch, caplog):
    from api.routes.admin import purchases as admin_routes   # the module whose _gateway the review routes call
    gw = FakeGateway(lookup_state="not_found")
    gateway_calls = []
    monkeypatch.setattr(admin_routes, "_gateway", lambda: gateway_calls.append(1) or gw)
    H = auth_headers(admin)
    p = await park(db, user, bitget_key)
    async with api() as c:
        with caplog.at_level(logging.INFO):
            r = await c.post(f"/api/admin/purchases/{p.id}/resolve", headers=H,
                             json={"action": "mark_not_sent", "note": "no order with this client id on the exchange"})
        assert r.status_code == 200 and r.json()["status"] == "active"
    p = await reload(db, p)
    assert p.next_execution is not None and p.failure_count == 0
    (tx,) = await fresh_rows(db, p.id)
    assert tx.status == "failed" and "[admin: not sent (not_found)" in tx.error_message
    rec = next(x for x in caplog.records if getattr(x, "action", None) == "purchase_review_resolved")
    assert rec.exchange_state == "not_found" and rec.note.startswith("no order")
    # the next run buys fresh (the failed row is not pending, so nothing to reconcile) — exactly once
    from datetime import datetime, timezone
    gw = FakeGateway()
    counts = await ps.execute_due_purchases(db, now=datetime.now(timezone.utc) + timedelta(minutes=1), gateway=gw, dry_run=True)
    assert counts["executed"] == 1 and len(gw.placed) == 1
    # cancel
    p2 = await park(db, user, bitget_key)
    async with api() as c:
        r = await c.post(f"/api/admin/purchases/{p2.id}/resolve", headers=H, json={"action": "cancel", "note": "customer asked us to stop"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    p2 = await reload(db, p2)
    assert p2.status == "cancelled" and p2.next_execution is None and "cancelled by admin" in p2.last_error
    assert gateway_calls, "the resolve route used the patched gateway, not the real one"
