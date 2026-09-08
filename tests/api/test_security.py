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
    enforce_production_secrets(Settings(_env_file=None))  # no raise


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


async def test_logout_revokes_refresh_token(client):
    from api.routes.auth import _create_refresh_token
    refresh = _create_refresh_token(str(uuid.uuid4()))
    async with client as c:
        assert (await c.post("/api/auth/logout",
                             json={"refresh_token": refresh})).status_code == 204
        r = await c.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert r.status_code == 401, "revoked refresh token must be rejected"


async def test_logout_rejects_access_token(client, user):
    from api.auth import create_access_token
    async with client as c:
        r = await c.post("/api/auth/logout",
                         json={"refresh_token": create_access_token(str(user.id)).access_token})
    assert r.status_code == 401


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
            assert (await c.post("/api/auth/refresh",
                                 json={"refresh_token": old["refresh_token"]})).status_code == 401, \
                "pre-change refresh token must die"
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


async def test_no_secrets_in_logs(db, user, purchase, binance_key, monkeypatch, caplog):
    from api.services import purchase_service as ps_mod
    from api.services.purchase_service import PurchaseService
    from tests.api.test_purchase_service import FakeBinance
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    with caplog.at_level(logging.DEBUG):
        tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"
    assert "test-api-key-000001" not in caplog.text, "plaintext API key leaked into logs"
    assert "test-api-secret-01" not in caplog.text
