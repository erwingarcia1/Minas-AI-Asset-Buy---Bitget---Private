"""Admin pause switch for BOTH schedulers, the /health scheduler report and the
Auto-Buy alerts. The switch lives in Redis so it applies to every gunicorn
worker and survives an API restart; reads fail closed."""
import logging
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from api.models import User
from api.services import scheduler_state
from api.services.recurring_scheduler import RecurringScheduler
from api.tasks import purchase_scheduler as ps
from tests.api.conftest import auth_headers
from tests.conftest_autobuy import binance_key, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.test_purchase_scheduler import FailingGateway, FakeGateway, NOW


def api():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def admin(db):
    u = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
             password_hash="x", role="admin", kyc_status="verified")
    db.add(u)
    await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh:
        await db.delete(fresh)
        await db.commit()


async def _clear_scheduler_state():
    from api.services.redis_client import get_redis
    await scheduler_state.resume()
    for name in scheduler_state.SCHEDULERS:
        await get_redis().delete(f"autobuy:schedulers:{name}:last_run",
                                 f"autobuy:schedulers:{name}:consecutive_failures")


@pytest.fixture(autouse=True)
async def clean_scheduler_state():
    """The switch, heartbeats and counters are global Redis state: start each
    test clean and never leak a pause into the rest of the suite."""
    await _clear_scheduler_state()
    yield
    await _clear_scheduler_state()


# --------------------------------------------------------------------------- Auto-Buy job

async def test_pause_makes_autobuy_skip_and_resume_restores(db, user, binance_key, admin, caplog):
    p = await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(minutes=1))
    gw = FakeGateway()
    async with api() as c:
        assert (await c.post("/api/admin/scheduler/pause", headers=auth_headers(admin))).status_code == 204

    with caplog.at_level(logging.WARNING, logger="autobuy.tasks.purchases"):
        counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert counts == {"executed": 0, "failed": 0, "skipped": 0}
    assert gw.calls == [], "nothing may reach the exchange while paused"
    assert any("PAUSED" in r.getMessage() and str(admin.id) in r.getMessage() for r in caplog.records)
    await db.refresh(p)
    assert p.next_execution == NOW - timedelta(minutes=1), "the purchase stays due, untouched"
    hb = await scheduler_state.last_run("autobuy")
    assert hb["result"] == "skipped_paused" and str(admin.id) in hb["detail"]

    async with api() as c:
        assert (await c.post("/api/admin/scheduler/resume", headers=auth_headers(admin))).status_code == 204
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
    assert counts["executed"] == 1 and len(gw.calls) == 1
    hb = await scheduler_state.last_run("autobuy")
    assert hb["result"] == "ok" and hb["counts"]["executed"] == 1
    assert await scheduler_state.consecutive_failures("autobuy") == 0


async def test_sync_entry_point_skips_when_paused(db, user, binance_key, caplog):
    """execute_purchases() (what APScheduler calls) honours the switch too."""
    p = await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(hours=1))
    import asyncio
    await scheduler_state.pause("admin-sync-test")
    with caplog.at_level(logging.WARNING, logger="autobuy.tasks.purchases"):
        counts = await asyncio.to_thread(ps.execute_purchases)   # APScheduler calls it from a thread
    assert counts == {"executed": 0, "failed": 0, "skipped": 0}
    assert any("PAUSED" in r.getMessage() for r in caplog.records)
    await db.refresh(p)
    assert p.next_execution == NOW - timedelta(hours=1) and p.failure_count == 0


async def test_redis_unreadable_fails_closed(db, user, binance_key, monkeypatch, caplog):
    await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(minutes=1))
    gw = FakeGateway()

    async def boom():
        raise scheduler_state.PauseStateUnavailable("connection refused")
    monkeypatch.setattr(scheduler_state, "pause_info", boom)

    with caplog.at_level(logging.ERROR):
        counts = await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=True)
        tick = await RecurringScheduler(dry_run=True).tick()
    assert counts == {"executed": 0, "failed": 0, "skipped": 0} and gw.calls == []
    assert tick == 0
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert sum("failing closed" in m for m in msgs) == 2, msgs


async def test_failed_runs_are_counted_and_reset(db, user, binance_key):
    await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(minutes=1))
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    assert (await scheduler_state.last_run("autobuy"))["result"] == "failed"
    assert await scheduler_state.consecutive_failures("autobuy") == 1
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    assert await scheduler_state.consecutive_failures("autobuy") == 2
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    assert await scheduler_state.consecutive_failures("autobuy") == 0
    assert (await scheduler_state.last_run("autobuy"))["result"] == "ok"


# --------------------------------------------------------------------------- legacy tick

async def test_legacy_tick_skips_when_paused(monkeypatch, caplog):
    await scheduler_state.pause("admin-legacy-test")
    s = RecurringScheduler(dry_run=True)

    async def must_not_run():
        raise AssertionError("the tick must not process purchases while paused")
    monkeypatch.setattr(s, "_process_due", must_not_run)

    with caplog.at_level(logging.WARNING, logger="autobuy.scheduler"):
        assert await s.tick() == 0
    assert any("PAUSED" in r.getMessage() for r in caplog.records)
    assert (await scheduler_state.last_run("recurring"))["result"] == "skipped_paused"
    await scheduler_state.resume()

    async def nothing_due():
        return 0
    monkeypatch.setattr(s, "_process_due", nothing_due)
    assert await s.tick() == 0  # ran again after resume
    assert (await scheduler_state.last_run("recurring"))["result"] == "ok"


# --------------------------------------------------------------------------- persistence

async def test_pause_state_survives_a_restart(admin):
    """The switch is only in Redis: a brand-new process (fresh connections, a
    fresh scheduler instance, the sync path the APScheduler thread uses) sees it."""
    from api.services.redis_client import close_redis
    async with api() as c:
        assert (await c.post("/api/admin/scheduler/pause", headers=auth_headers(admin))).status_code == 204
    await close_redis()                      # drop every in-process connection
    assert RecurringScheduler().running is False
    assert (await scheduler_state.pause_info())["paused"] is True      # fresh async client
    sync_view = scheduler_state.pause_info_sync()                        # fresh sync connection
    assert sync_view["paused"] is True and sync_view["by"] == str(admin.id) and sync_view["since"]
    async with api() as c:
        h = (await c.get("/health")).json()
    assert h["schedulers"]["paused"] is True and h["schedulers"]["paused_by"] == str(admin.id)
    assert h["schedulers"]["recurring"]["paused"] is True and h["schedulers"]["autobuy"]["paused"] is True


# --------------------------------------------------------------------------- health + alerts

async def test_health_reports_both_schedulers(db, user, binance_key):
    await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(minutes=1))
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    await RecurringScheduler(dry_run=True).tick()
    async with api() as c:
        h = (await c.get("/health")).json()
    s = h["schedulers"]
    assert s["paused"] is False and s["paused_since"] is None
    assert s["autobuy"]["last_result"] == "ok" and s["autobuy"]["last_run_at"]
    assert s["autobuy"]["last_counts"]["executed"] == 1 and s["autobuy"]["consecutive_failures"] == 0
    assert s["autobuy"]["interval_minutes"] == 5 and s["autobuy"]["dry_run"] is True
    assert s["recurring"]["last_result"] == "ok" and s["recurring"]["last_run_at"]
    assert s["recurring"]["consecutive_failures"] == 0
    assert set(s["autobuy"]) >= {"running", "paused", "last_run_at", "last_result", "last_detail"}
    assert h["scheduler"]["dry_run"] is True, "legacy key kept for existing monitors"


async def test_alerts_cover_the_autobuy_job(db, user, binance_key, admin):
    H = auth_headers(admin)
    await make_purchase(db, user, binance_key, next_execution=NOW - timedelta(minutes=1))
    await ps.execute_due_purchases(db, now=NOW, gateway=FailingGateway(), dry_run=True)
    async with api() as c:
        by = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=H)).json()}
    assert by["schedulers_paused"]["severity"] == "ok" and by["schedulers_paused"]["value"] == "running"
    assert by["autobuy_run"]["severity"] == "warning" and "failed" in by["autobuy_run"]["detail"]
    assert by["autobuy_consecutive_failures"]["value"] == "1"
    assert int(by["failed_transactions_24h"]["value"]) >= 1

    await scheduler_state.pause(str(admin.id))
    async with api() as c:
        by = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=H)).json()}
    assert by["schedulers_paused"]["severity"] == "warning" and str(admin.id) in by["schedulers_paused"]["detail"]


async def test_autobuy_run_alert_goes_critical_when_overdue(admin, monkeypatch):
    from datetime import datetime, timezone
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    async def stale_status(name):
        return {"last_run_at": stale, "last_result": "ok", "last_detail": None,
                "last_counts": None, "consecutive_failures": 0, "redis": "ok"}
    monkeypatch.setattr(scheduler_state, "status", stale_status)
    async with api() as c:
        by = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=auth_headers(admin))).json()}
    assert by["autobuy_run"]["severity"] == "critical"


async def test_non_admin_cannot_pause_or_resume(user):
    H = auth_headers(user)
    async with api() as c:
        assert (await c.post("/api/admin/scheduler/pause", headers=H)).status_code == 403
        assert (await c.post("/api/admin/scheduler/resume", headers=H)).status_code == 403
        assert (await c.post("/api/admin/scheduler/pause")).status_code == 401
    assert (await scheduler_state.pause_info())["paused"] is False
