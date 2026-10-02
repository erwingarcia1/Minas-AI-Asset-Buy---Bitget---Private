"""Refresh rotation grace: two tabs refreshing with the same cookie end up with ONE
identical new session (atomic claim, cached encrypted for 30 s); the grace covers
only the immediately previous token, expires, and is dropped by logout and by a
password change."""
import asyncio
import json

import pytest
from jose import jwt

from api.config import settings
from api.routes import auth as auth_mod
from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, GRACE_PREFIX, REFRESH_COOKIE, REVOKED_PREFIX
from tests.api.test_session_cookies import PW, account, client, cookie_attrs  # noqa: F401


def jti_of(token: str) -> str:
    return jwt.get_unverified_claims(token)["jti"]


def refresh_cookie(r) -> str:
    raw = next(h for h in r.headers.get_list("set-cookie") if h.startswith(f"{REFRESH_COOKIE}="))
    return raw.split(";", 1)[0].split("=", 1)[1]


async def login(c, acct):
    body = (await c.post("/api/auth/login", json={"email": acct.email, "password": PW})).json()
    return body, c.cookies[REFRESH_COOKIE]


def with_cookie(c, token, csrf):
    return dict(cookies={REFRESH_COOKIE: token, CSRF_COOKIE: csrf}, headers={CSRF_HEADER: csrf})


async def redis():
    from api.services.redis_client import get_redis
    return get_redis()


async def test_grace_hit_returns_the_identical_session_to_the_second_tab(account):
    async with client() as c:
        login_body, t0 = await login(c, account)
        first = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert first.status_code == 200
        second = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))   # same old cookie, inside the window
        assert second.status_code == 200, second.text
        assert second.json()["access_token"] == first.json()["access_token"]
        assert second.json()["csrf_token"] == first.json()["csrf_token"]
        assert refresh_cookie(second) == refresh_cookie(first), "one identical cookie set in both tabs"
        assert 0 < second.json()["expires_in"] <= first.json()["expires_in"]
        # the shared new token keeps working, and names the token it replaced
        claims = jwt.get_unverified_claims(refresh_cookie(first))
        assert claims["prv"] == jti_of(t0)
        nxt = await c.post("/api/auth/refresh", **with_cookie(c, refresh_cookie(first), first.json()["csrf_token"]))
        assert nxt.status_code == 200
        # the cached entry is encrypted: the token never sits in Redis in clear text
        raw = await (await redis()).get(GRACE_PREFIX + jti_of(t0))
        assert raw is None or refresh_cookie(first) not in (raw.decode() if isinstance(raw, bytes) else raw)


async def test_grace_entry_is_encrypted_and_expires(account, monkeypatch):
    monkeypatch.setattr(auth_mod, "REFRESH_GRACE_SECONDS", 1)
    async with client() as c:
        login_body, t0 = await login(c, account)
        first = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert first.status_code == 200
        raw = await (await redis()).get(GRACE_PREFIX + jti_of(t0))
        assert raw, "grace entry stored"
        text = raw.decode() if isinstance(raw, bytes) else raw
        assert refresh_cookie(first) not in text and first.json()["access_token"] not in text and "csrf" not in text.lower()
        from api.routes.exchange import decrypt_value
        assert json.loads(decrypt_value(text))["csrf_token"] == first.json()["csrf_token"], "decrypts with the app key"
        await asyncio.sleep(1.2)
        late = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert late.status_code == 401, "after the window the old token is refused as before"
        assert "max-age=0" in cookie_attrs(late, REFRESH_COOKIE) or "expires=" in cookie_attrs(late, REFRESH_COOKIE), "a real refusal clears the cookies"


async def test_concurrent_refreshes_claim_atomically_and_mint_one_token_chain(account):
    async with client() as c1, client() as c2:
        login_body, t0 = await login(c1, account)
        kw = with_cookie(c1, t0, login_body["csrf_token"])
        a, b = await asyncio.gather(c1.post("/api/auth/refresh", **kw), c2.post("/api/auth/refresh", **kw))
        assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
        assert a.json()["access_token"] == b.json()["access_token"]
        assert a.json()["csrf_token"] == b.json()["csrf_token"]
        assert refresh_cookie(a) == refresh_cookie(b), "exactly one new refresh token exists"
        r = await redis()
        marker = await r.get(REVOKED_PREFIX + jti_of(t0))
        float(marker.decode() if isinstance(marker, bytes) else marker)   # the claim marker is the rotation time
        assert await r.exists(GRACE_PREFIX + jti_of(t0)) == 1
        # both tabs now hold one token; it rotates once more as usual
        nxt = await c2.post("/api/auth/refresh", **with_cookie(c2, refresh_cookie(a), a.json()["csrf_token"]))
        assert nxt.status_code == 200


async def test_grace_covers_only_the_immediately_previous_token(account):
    async with client() as c:
        login_body, t0 = await login(c, account)
        r1 = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        t1 = refresh_cookie(r1)
        r2 = await c.post("/api/auth/refresh", **with_cookie(c, t1, r1.json()["csrf_token"]))
        assert r2.status_code == 200
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(t0)) == 0, "dropped by the next rotation"
        old = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert old.status_code == 401, "two rotations old: refused as today"
        still = await c.post("/api/auth/refresh", **with_cookie(c, t1, r1.json()["csrf_token"]))
        assert still.status_code == 200, "one rotation old: grace"


async def test_logout_inside_the_window_drops_the_grace_entry(account):
    async with client() as c:
        login_body, t0 = await login(c, account)
        r1 = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        t1, csrf1 = refresh_cookie(r1), r1.json()["csrf_token"]
        out = await c.post("/api/auth/logout", **with_cookie(c, t1, csrf1))
        assert out.status_code == 204
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(t0)) == 0
        assert (await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))).status_code == 401, "the old tab cannot undo the logout"
        assert (await c.post("/api/auth/refresh", **with_cookie(c, t1, csrf1))).status_code == 401, "the logged-out token is dead too"


async def test_password_change_inside_the_window_drops_the_grace_entry(account):
    async with client() as c:
        login_body, t0 = await login(c, account)
        r1 = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        t1 = refresh_cookie(r1)
        ok = await c.post("/api/auth/change-password", headers={"Authorization": f"Bearer {r1.json()['access_token']}"},
                          cookies={REFRESH_COOKIE: t1}, json={"current_password": PW, "new_password": "Changed-Passw0rd!2"})
        assert ok.status_code == 204
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(t0)) == 0
        assert (await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))).status_code == 401
        assert (await c.post("/api/auth/refresh", **with_cookie(c, t1, r1.json()["csrf_token"]))).status_code == 401


async def test_password_change_from_another_tab_is_caught_by_the_issue_time_check(account, db):
    """The grace entry is only dropped by the request that carries the cookie; a
    change that reaches the user row another way is caught by issued_at."""
    from datetime import datetime, timezone
    async with client() as c:
        login_body, t0 = await login(c, account)
        r1 = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert r1.status_code == 200
        account.password_changed_at = datetime.now(timezone.utc)
        await db.commit()
        late = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert late.status_code == 401
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(t0)) == 0


async def test_nothing_secret_is_logged_on_the_grace_path(account, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    async with client() as c:
        login_body, t0 = await login(c, account)
        r1 = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        r2 = await c.post("/api/auth/refresh", **with_cookie(c, t0, login_body["csrf_token"]))
        assert r2.status_code == 200
    everything = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for secret in (t0, refresh_cookie(r1), r1.json()["access_token"], r1.json()["csrf_token"]):
        assert secret not in everything
    assert "refresh served from the rotation grace window" in everything


# --- tokens issued before this change (no "prv" claim) ------------------------------------

def mint_old_style(user_id: str) -> str:
    """Exactly what _create_refresh_token produced before the grace window: no prv claim."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "type": "refresh", "jti": uuid4().hex, "iat": now,
                       "exp": now + timedelta(days=settings.refresh_token_expire_days)},
                      settings.secret_key, algorithm=settings.algorithm)


async def test_pre_deploy_token_without_prv_refreshes_gets_grace_and_is_dropped_by_logout(account):
    old = mint_old_style(str(account.id))
    assert "prv" not in jwt.get_unverified_claims(old)
    async with client() as c:
        # (a) refreshes normally on the new code
        first = await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))
        assert first.status_code == 200, first.text
        new_token = refresh_cookie(first)
        assert jwt.get_unverified_claims(new_token)["prv"] == jti_of(old), "the new token names the old one"
        # (b) the grace window applies like any other token
        second = await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))
        assert second.status_code == 200 and second.json()["access_token"] == first.json()["access_token"]
        assert refresh_cookie(second) == new_token
        # (c) logout with the NEW token drops the grace entry of the old one
        out = await c.post("/api/auth/logout", **with_cookie(c, new_token, first.json()["csrf_token"]))
        assert out.status_code == 204
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(old)) == 0
        assert (await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))).status_code == 401
        assert (await c.post("/api/auth/refresh", **with_cookie(c, new_token, first.json()["csrf_token"]))).status_code == 401


async def test_pre_deploy_token_logout_with_the_old_token_itself_leaves_no_grace(account):
    old = mint_old_style(str(account.id))
    async with client() as c:
        first = await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))
        assert first.status_code == 200
        # a tab that still holds the OLD cookie signs out: its marker becomes "logout", no grace survives
        out = await c.post("/api/auth/logout", **with_cookie(c, old, "csrf-old"))
        assert out.status_code == 204
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(old)) == 0
        marker = await (await redis()).get(REVOKED_PREFIX + jti_of(old))
        assert (marker.decode() if isinstance(marker, bytes) else marker) == "logout"
        assert (await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))).status_code == 401


async def test_pre_deploy_token_is_dropped_by_a_password_change(account):
    old = mint_old_style(str(account.id))
    async with client() as c:
        first = await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))
        assert first.status_code == 200
        new_token = refresh_cookie(first)
        ok = await c.post("/api/auth/change-password", headers={"Authorization": f"Bearer {first.json()['access_token']}"},
                          cookies={REFRESH_COOKIE: new_token}, json={"current_password": PW, "new_password": "Changed-Passw0rd!2"})
        assert ok.status_code == 204
        assert await (await redis()).exists(GRACE_PREFIX + jti_of(old)) == 0
        assert (await c.post("/api/auth/refresh", **with_cookie(c, old, "csrf-old"))).status_code == 401
        assert (await c.post("/api/auth/refresh", **with_cookie(c, new_token, first.json()["csrf_token"]))).status_code == 401
