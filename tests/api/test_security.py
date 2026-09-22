import logging
import uuid

import pytest

from api.config import Settings, enforce_production_secrets, settings
from tests.api.conftest import auth_headers

PW = "Str0ng!passw0rd"
NEW_PW = "N3w!passw0rd$"


def test_production_guard_refuses_default_secrets(monkeypatch):
    monkeypatch.setenv("API_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "your-secret-key-change-this")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    # Field defaults are captured from os.environ at import time, so the developer's .env
    # would leak in through them: pass the guarded fields explicitly instead.
    s = Settings(_env_file=None, encryption_key="")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        enforce_production_secrets(s)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    s = Settings(_env_file=None, encryption_key="")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        enforce_production_secrets(s)
    monkeypatch.setenv("ENCRYPTION_KEY", "y" * 40)
    # the guard also requires production email + link configuration (tests blank RESEND_API_KEY)
    enforce_production_secrets(Settings(_env_file=None, resend_api_key="re_live_ok",
                                        resend_from_email="Minas AutoBuy <noreply@example.com>",
                                        app_base_url="https://app.example.com", log_reset_links=""))  # no raise


def test_guard_is_noop_in_development():
    enforce_production_secrets(settings)


def test_fernet_uses_independent_encryption_key(monkeypatch):
    from api.routes.exchange import decrypt_value, encrypt_value
    from cryptography.fernet import InvalidToken
    token_under_secret = encrypt_value("payload-123")
    monkeypatch.setattr(settings, "encryption_key", "independent-key-material")
    token_under_enc = encrypt_value("payload-123")
    assert decrypt_value(token_under_enc) == "payload-123"
    import api.routes.exchange as ex
    with pytest.raises(Exception):
        ex._fernet().decrypt(token_under_secret.encode())  # old cipher no longer valid


async def test_security_headers_present(client):
    async with client as c:
        r = await c.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in r.headers  # dev: no HSTS


async def test_hsts_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "api_env", "production")
    async with client as c:
        r = await c.get("/health")
    assert "max-age=31536000" in r.headers["strict-transport-security"]


async def test_logout_revokes_refresh_token(client, user):
    from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE, _create_refresh_token
    refresh = _create_refresh_token(str(user.id))
    cookies = {REFRESH_COOKIE: refresh, CSRF_COOKIE: "csrf-1"}
    async with client as c:
        assert (await c.post("/api/auth/logout", cookies=cookies, headers={CSRF_HEADER: "csrf-1"})).status_code == 204
        r = await c.post("/api/auth/refresh", cookies=cookies, headers={CSRF_HEADER: "csrf-1"})
        assert r.status_code == 401, "revoked refresh token must be rejected"


async def test_logout_ignores_an_access_token_in_the_cookie(client, user):
    from api.auth import create_access_token
    from api.routes.auth import CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
    async with client as c:
        r = await c.post("/api/auth/logout", cookies={REFRESH_COOKIE: create_access_token(str(user.id)).access_token, CSRF_COOKIE: "x"},
                         headers={CSRF_HEADER: "x"})
    assert r.status_code == 204, "nothing valid to revoke; the cookies are cleared anyway"


async def test_password_change_invalidates_old_tokens(db):
    import httpx
    from sqlalchemy import select
    from api.main import app
    from api.models import User
    email = f"pwchange-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            assert (await c.post("/api/auth/register",
                                 json={"email": email, "password": PW})).status_code == 201
            old = (await c.post("/api/auth/login",
                                json={"email": email, "password": PW})).json()
            H_old = {"Authorization": f"Bearer {old['access_token']}"}

            wrong = await c.post("/api/auth/change-password", headers=H_old,
                                 json={"current_password": "Wrong1!pass", "new_password": NEW_PW})
            assert wrong.status_code == 401
            weak = await c.post("/api/auth/change-password", headers=H_old,
                                json={"current_password": PW, "new_password": "weakpass"})
            assert weak.status_code == 422

            import asyncio
            await asyncio.sleep(1.1)  # ensure iat < password_changed_at at 1s resolution
            ok = await c.post("/api/auth/change-password", headers=H_old,
                              json={"current_password": PW, "new_password": NEW_PW})
            assert ok.status_code == 204

            assert (await c.get("/api/user/profile", headers=H_old)).status_code == 401, \
                "pre-change access token must die"
            from api.routes.auth import CSRF_HEADER
            assert (await c.post("/api/auth/refresh", headers={CSRF_HEADER: old["csrf_token"]})).status_code == 401, \
                "pre-change refresh cookie (kept by the client) must die"
            assert (await c.post("/api/auth/login",
                                 json={"email": email, "password": PW})).status_code == 401
            relogin = await c.post("/api/auth/login",
                                   json={"email": email, "password": NEW_PW})
            assert relogin.status_code == 200
    finally:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u:
            await db.delete(u)
            await db.commit()


async def test_no_secrets_in_logs(db, user, binance_key, monkeypatch, caplog):
    """A full Auto-Buy execution through the real gateway (exchange stubbed) never logs key material."""
    from datetime import timedelta
    from api.models import utcnow
    from api.services import exchange_client as bc
    from api.tasks import purchase_scheduler as ps
    from tests.api.conftest import make_autobuy_purchase
    from tests.test_purchase_scheduler import FakeLegacy
    monkeypatch.setattr(bc, "_LegacyClient", FakeLegacy)
    await make_autobuy_purchase(db, user, key=binance_key, purchase_type="dca",
                                next_execution=utcnow() - timedelta(minutes=1))
    with caplog.at_level(logging.DEBUG):
        counts = await ps.execute_due_purchases(db, gateway=ps.ExchangeClientGateway(), dry_run=True)
    assert counts["executed"] == 1
    assert "test-api-key-000001" not in caplog.text, "plaintext API key leaked into logs"
    assert "test-api-secret-01" not in caplog.text
