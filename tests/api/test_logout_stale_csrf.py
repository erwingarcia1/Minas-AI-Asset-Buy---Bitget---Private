"""Issue #64: a tab whose CSRF value went stale (another tab rotated the cookies) must still be
able to sign out FOR REAL. Logout revokes whenever the refresh cookie holds a valid token, whatever
the CSRF header says; without a valid cookie the CSRF check applies as before."""
from jose import jwt

from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, GRACE_PREFIX, REFRESH_COOKIE, REVOKED_PREFIX
from tests.api.test_refresh_grace import jti_of, login, redis, refresh_cookie, with_cookie
from tests.api.test_session_cookies import PW, account, client  # noqa: F401


async def test_logout_with_rotated_cookies_and_a_stale_header_revokes_everything(account):
    async with client() as c:
        login_body, t0 = await login(c, account)
        csrf0 = login_body["csrf_token"]
        r1 = await c.post("/api/auth/refresh", **with_cookie(c, t0, csrf0))      # the other tab rotated
        t1, csrf1 = refresh_cookie(r1), r1.json()["csrf_token"]
        # the stale tab: rotated cookies attached by the browser, old CSRF value in the header
        out = await c.post("/api/auth/logout", cookies={REFRESH_COOKIE: t1, CSRF_COOKIE: csrf1}, headers={CSRF_HEADER: csrf0})
        assert out.status_code == 204
        cleared = [h.lower() for h in out.headers.get_list("set-cookie")]
        assert any(h.startswith(f"{REFRESH_COOKIE}=") and ("max-age=0" in h or "expires=" in h) for h in cleared)
        r = await redis()
        marker = await r.get(REVOKED_PREFIX + jti_of(t1))
        assert (marker.decode() if isinstance(marker, bytes) else marker) == "logout", "revoked server-side, no grace"
        assert await r.exists(GRACE_PREFIX + jti_of(t0)) == 0 and await r.exists(GRACE_PREFIX + jti_of(t1)) == 0
        assert (await c.post("/api/auth/refresh", **with_cookie(c, t1, csrf1))).status_code == 401, "the current cookie set is dead"
        assert (await c.post("/api/auth/refresh", **with_cookie(c, t0, csrf0))).status_code == 401, "and so is the previous one"


async def test_logout_with_a_valid_cookie_and_no_header_at_all_also_revokes(account):
    async with client() as c:
        login_body, t0 = await login(c, account)
    async with client() as fresh:
        out = await fresh.post("/api/auth/logout", cookies={REFRESH_COOKIE: t0, CSRF_COOKIE: login_body["csrf_token"]})
        assert out.status_code == 204
        assert (await fresh.post("/api/auth/refresh", **with_cookie(fresh, t0, login_body["csrf_token"]))).status_code == 401


async def test_logout_without_a_valid_cookie_still_needs_the_csrf_pair(account):
    async with client() as fresh:
        assert (await fresh.post("/api/auth/logout")).status_code == 403
        assert (await fresh.post("/api/auth/logout", cookies={REFRESH_COOKIE: "garbage", CSRF_COOKIE: "c"}, headers={CSRF_HEADER: "d"})).status_code == 403
        ok = await fresh.post("/api/auth/logout", cookies={CSRF_COOKIE: "c"}, headers={CSRF_HEADER: "c"})
        assert ok.status_code == 204, "nothing to revoke, header matches: 204 as before"
