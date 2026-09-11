import uuid

import httpx
import pytest
from sqlalchemy import select

from api.models import User
from tests.api.conftest import auth_headers, make_autobuy_purchase, make_history

PW = "Str0ng!passw0rd"


def client():
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


ADMIN_ROUTES = [
    # every route registered on api/routes/admin.py — each depends on require_admin
    ("get", "/api/admin/stats"),
    ("get", "/api/admin/users"),
    ("post", "/api/admin/users/{uid}/suspend"),
    ("post", "/api/admin/users/{uid}/unsuspend"),
    ("delete", "/api/admin/users/{uid}?confirm=x@example.com"),
    ("post", "/api/admin/users/{uid}/kyc-override"),
    ("get", "/api/admin/transactions"),
    ("post", "/api/admin/purchases/{uid}/force-retry"),
    ("get", "/api/admin/alerts"),
    ("post", "/api/admin/scheduler/pause"),
    ("post", "/api/admin/scheduler/resume"),
]


def test_every_admin_route_is_guarded_by_require_admin():
    """Static check: the guard is a dependency on every route in the admin router,
    so a new route cannot be added without it showing up here."""
    from api.routes import admin as admin_routes
    from fastapi.routing import APIRoute

    routes = [r for r in admin_routes.router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(ADMIN_ROUTES)
    for r in routes:
        guards = [d.call for d in r.dependant.dependencies]
        assert admin_routes.require_admin in guards, f"{r.path} lacks require_admin"


async def test_non_admin_gets_403_on_every_admin_route(db, user):
    H = auth_headers(user)
    uid = uuid.uuid4()
    async with client() as c:
        for method, path in ADMIN_ROUTES:
            kwargs = {"headers": H}
            if "kyc-override" in path:
                kwargs["json"] = {"status": "verified", "note": "manual review done"}
            r = await getattr(c, method)(path.format(uid=uid), **kwargs)
            assert r.status_code == 403, (method, path, r.status_code, r.text)
            assert r.json()["detail"] == "Admin access required"


async def test_unauthenticated_gets_401_on_every_admin_route():
    uid = uuid.uuid4()
    async with client() as c:
        for method, path in ADMIN_ROUTES:
            r = await getattr(c, method)(path.format(uid=uid))
            assert r.status_code == 401, (method, path, r.status_code)


async def test_stats_and_user_listing(db, admin, user, binance_key):
    await make_history(db, user, crypto="BTC", amount_crypto=0.001, cost_usd=123.0, key=binance_key)
    H = auth_headers(admin)
    async with client() as c:
        stats = (await c.get("/api/admin/stats", headers=H)).json()
        assert stats["total_users"] >= 2
        assert stats["total_invested_usdt"] >= 123.0
        assert stats["active_purchases"] >= 1

        listed = (await c.get("/api/admin/users",
                              params={"q": user.email[:12]}, headers=H)).json()
        assert listed["total"] == 1
        assert listed["items"][0]["invested_usdt"] == 123.0


async def test_suspend_blocks_user_and_unsuspend_restores(db, admin, user):
    H_admin = auth_headers(admin)
    H_user = auth_headers(user)
    async with client() as c:
        assert (await c.get("/api/user/profile", headers=H_user)).status_code == 200
        assert (await c.post(f"/api/admin/users/{user.id}/suspend",
                             headers=H_admin)).status_code == 204
        assert (await c.get("/api/user/profile", headers=H_user)).status_code == 403, \
            "suspended users lose API access immediately"
        assert (await c.post(f"/api/admin/users/{user.id}/unsuspend",
                             headers=H_admin)).status_code == 204
        assert (await c.get("/api/user/profile", headers=H_user)).status_code == 200


async def test_admin_cannot_suspend_or_delete_self(admin):
    H = auth_headers(admin)
    async with client() as c:
        assert (await c.post(f"/api/admin/users/{admin.id}/suspend",
                             headers=H)).status_code == 400
        assert (await c.delete(f"/api/admin/users/{admin.id}",
                               params={"confirm": admin.email},
                               headers=H)).status_code == 400


async def test_delete_requires_email_confirmation(db, admin):
    victim = User(email=f"victim-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.add(victim)
    await db.commit()
    H = auth_headers(admin)
    async with client() as c:
        r = await c.delete(f"/api/admin/users/{victim.id}",
                           params={"confirm": "wrong@example.com"}, headers=H)
        assert r.status_code == 400
        r = await c.delete(f"/api/admin/users/{victim.id}",
                           params={"confirm": victim.email}, headers=H)
        assert r.status_code == 204
    assert (await db.execute(select(User).where(
        User.id == victim.id))).scalar_one_or_none() is None


async def test_kyc_override_requires_note(db, admin, user):
    H = auth_headers(admin)
    async with client() as c:
        bad = await c.post(f"/api/admin/users/{user.id}/kyc-override", headers=H,
                           json={"status": "verified", "note": "ok"})
        assert bad.status_code == 422  # note too short
        good = await c.post(f"/api/admin/users/{user.id}/kyc-override", headers=H,
                            json={"status": "rejected",
                                  "note": "documents mismatch per support ticket 4711"})
        assert good.status_code == 204
    await db.refresh(user)
    assert user.kyc_status == "rejected"


async def test_transactions_audit_and_force_retry(db, admin, user, binance_key):
    await make_history(db, user, crypto="BTC", amount_crypto=0.001, cost_usd=50.0, status="failed",
                       key=binance_key, error_message="boom")
    stuck = await make_autobuy_purchase(db, user, crypto="ETH", key=binance_key, status="failed",
                                        failure_count=3, next_execution=None)
    H = auth_headers(admin)
    async with client() as c:
        log = (await c.get("/api/admin/transactions",
                           params={"status_filter": "failed",
                                   "user_id": str(user.id)}, headers=H)).json()
        assert log["total"] == 1 and log["items"][0]["error_message"] == "boom"
        assert (await c.post(f"/api/admin/purchases/{stuck.id}/force-retry",
                             headers=H)).status_code == 204
        assert (await c.post(f"/api/admin/purchases/{uuid.uuid4()}/force-retry",
                             headers=H)).status_code == 404
    await db.refresh(stuck)
    from datetime import datetime, timezone
    assert stuck.status == "active" and stuck.failure_count == 0
    assert stuck.next_execution <= datetime.now(timezone.utc)
    done = await make_autobuy_purchase(db, user, crypto="SOL", key=binance_key, status="completed", next_execution=None)
    async with client() as c:
        assert (await c.post(f"/api/admin/purchases/{done.id}/force-retry", headers=H)).status_code == 409


async def test_alerts_snapshot(admin):
    H = auth_headers(admin)
    async with client() as c:
        items = (await c.get("/api/admin/alerts", headers=H)).json()
    names = {a["name"] for a in items}
    assert names == {"schedulers_paused", "autobuy_run", "autobuy_consecutive_failures",
                     "failed_transactions_24h"}, "only the Auto-Buy job's alerts remain"
    assert all(a["severity"] in ("ok", "warning", "critical") for a in items)
