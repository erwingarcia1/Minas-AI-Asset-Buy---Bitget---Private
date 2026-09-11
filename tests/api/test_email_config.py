"""Email is config-only, safe in production, and never leaks dev behaviour."""
import logging
import pathlib
import re
import uuid

import httpx
import pytest
from sqlalchemy import select

import api.routes.auth as auth_mod
from api.auth import hash_password
from api.config import Settings, email_config_problems, enforce_production_secrets, settings
from api.integrations import resend
from api.integrations.resend import ResendError, ResendNotConfigured
from api.models import User
from api.services.email_service import EmailService
from api.services.email_templates import TEMPLATES, render
from api.utils.links import build_app_link
from tests.api.conftest import auth_headers

FROM = "Minas AutoBuy <noreply@example.test>"
GOOD = dict(_env_file=None, api_env="production", secret_key="s" * 40, encryption_key="e" * 40,
            resend_api_key="re_live_ok", resend_from_email=FROM, app_base_url="https://app.example.test")


class Recorder:
    def __init__(self, status=200, body=None):
        self.requests, self.status, self.body = [], status, (body if body is not None else {"id": "email-1"})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return httpx.Response(self.status, json=self.body)

    def last(self):
        import json
        return json.loads(self.requests[-1].content)


@pytest.fixture
def mail(monkeypatch):
    """Configured Resend with a recording transport; no Reply-To sources set."""
    rec = Recorder()
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", FROM)
    monkeypatch.delenv("RESEND_REPLY_TO", raising=False)
    monkeypatch.delenv("SUPPORT_EMAIL", raising=False)
    monkeypatch.setattr(settings, "resend_reply_to", "")
    monkeypatch.setattr(settings, "support_email", "")
    monkeypatch.setattr(resend, "_transport", httpx.MockTransport(rec))
    return rec


# --------------------------------------------------------------------------- sender / reply-to

async def test_sender_comes_only_from_config(mail, monkeypatch):
    await resend.send_email("to@example.test", "Hi", "<p>x</p>")
    assert mail.last()["from"] == FROM
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Other <hello@example.test>")   # rotated without a restart
    await resend.send_email("to@example.test", "Hi", "<p>x</p>")
    assert mail.last()["from"] == "Other <hello@example.test>"
    assert not hasattr(resend, "DEFAULT_FROM_EMAIL"), "no built-in sender may exist"


async def test_reply_to_precedence(mail, monkeypatch):
    await resend.send_email("to@example.test", "Hi", "<p>x</p>")
    assert "reply_to" not in mail.last()
    monkeypatch.setattr(settings, "support_email", "help@example.test")
    await resend.send_email("to@example.test", "Hi", "<p>x</p>")
    assert mail.last()["reply_to"] == "help@example.test", "SUPPORT_EMAIL is the Reply-To fallback"
    monkeypatch.setenv("RESEND_REPLY_TO", "support@example.test")
    await resend.send_email("to@example.test", "Hi", "<p>x</p>")
    assert mail.last()["reply_to"] == "support@example.test", "RESEND_REPLY_TO wins when set"


async def test_missing_sender_skips_with_warning(mail, monkeypatch, caplog):
    monkeypatch.setenv("RESEND_FROM_EMAIL", "")
    monkeypatch.setattr(settings, "resend_from_email", "")
    caplog.set_level(logging.WARNING, logger="autobuy.integrations.resend")
    with pytest.raises(ResendNotConfigured):
        await resend.send_email("to@example.test", "Subject here", "<p>x</p>")
    assert mail.requests == []
    rec = [r for r in caplog.records if "email skipped (not configured" in r.getMessage()][0]
    assert rec.levelname == "WARNING" and "RESEND_FROM_EMAIL" in rec.getMessage()
    assert "to=to@example.test" in rec.getMessage() and "Subject here" in rec.getMessage()


async def test_placeholder_key_skips_with_warning_and_does_not_crash(mail, monkeypatch, caplog):
    monkeypatch.setenv("RESEND_API_KEY", "your_resend_api_key_here")
    caplog.set_level(logging.WARNING, logger="autobuy.integrations.resend")
    assert not resend.is_configured()
    assert await EmailService()._send("to@example.test", "Welcome", "<p>x</p>", "x") is False
    assert mail.requests == []
    assert any(r.levelname == "WARNING" and "email skipped (not configured: RESEND_API_KEY)" in r.getMessage()
               and "to=to@example.test" in r.getMessage() and "Welcome" in r.getMessage() for r in caplog.records)


async def test_delivery_failure_logs_error_with_status_and_reason(mail, monkeypatch, caplog):
    mail.status, mail.body = 403, {"statusCode": 403, "message": "The example.test domain is not verified"}
    caplog.set_level(logging.ERROR, logger="autobuy.integrations.resend")
    assert await EmailService()._send("to@example.test", "Hi", "<p>x</p>", "x") is False, "wrapper never raises"
    rec = [r for r in caplog.records if r.name == "autobuy.integrations.resend"][-1]
    assert rec.levelname == "ERROR" and "status 403" in rec.getMessage() and "not verified" in rec.getMessage()
    with pytest.raises(ResendError) as ei:
        await resend.send_email("to@example.test", "Hi", "<p>x</p>")
    assert ei.value.status == 403


# --------------------------------------------------------------------------- links

def test_build_app_link_uses_app_base_url_and_each_fallback_level(monkeypatch):
    monkeypatch.setattr(settings, "api_env", "development")
    monkeypatch.setattr(settings, "app_base_url", "https://app.example.test/")
    assert build_app_link("/reset-password", token="a b") == "https://app.example.test/reset-password?token=a+b"
    monkeypatch.setattr(settings, "app_base_url", "")
    monkeypatch.setattr(settings, "allowed_origins_csv", "https://dash.example.test/,https://other.example.test")
    assert build_app_link("verify-email", token="t") == "https://dash.example.test/verify-email?token=t"
    monkeypatch.setattr(settings, "allowed_origins_csv", "")
    assert build_app_link("/x") == "http://localhost:5173/x"
    monkeypatch.setattr(settings, "api_env", "production")
    with pytest.raises(RuntimeError, match="APP_BASE_URL"):
        build_app_link("/x")


async def test_spoofed_host_header_never_changes_emailed_links(client, db, monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://app.example.test")
    sent = []

    async def fake_send(to_email, subject, html_content, from_email=None, **kw):
        sent.append(kw.get("text_content", "")); return "e1"
    monkeypatch.setattr(resend, "send_email", fake_send)
    u = User(email=f"spoof-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password("Old-Passw0rd!"))
    db.add(u); await db.commit()
    try:
        async with client as c:
            r = await c.post("/api/auth/forgot-password", json={"email": u.email},
                             headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example",
                                      "Origin": "https://evil.example", "Referer": "https://evil.example/"})
        assert r.status_code == 200
        link = re.search(r"https?://\S+", sent[0]).group(0)
        assert link.startswith("https://app.example.test/reset-password?token=")
        assert "evil.example" not in sent[0]
    finally:
        await db.delete(u); await db.commit()


# --------------------------------------------------------------------------- dev behaviour

async def test_reset_link_logging_is_off_by_default_and_explicit_opt_in(client, db, monkeypatch, caplog):
    monkeypatch.setattr(resend, "send_email", lambda *a, **k: _ok())
    u = User(email=f"loglink-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password("Old-Passw0rd!"))
    db.add(u); await db.commit()
    caplog.set_level(logging.INFO, logger="autobuy.auth")
    try:
        assert settings.reset_links_logged is False, "LOG_RESET_LINKS must default to off"
        monkeypatch.setattr(settings, "api_env", "development")   # dev alone must NOT enable it
        async with client as c:
            await c.post("/api/auth/forgot-password", json={"email": u.email})
        assert not any("password reset link" in r.getMessage() for r in caplog.records)
        monkeypatch.setattr(settings, "log_reset_links", "1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=__import__("api.main").main.app), base_url="http://test") as c:
            await c.post("/api/auth/forgot-password", json={"email": u.email})
        hits = [r for r in caplog.records if "password reset link (LOG_RESET_LINKS=1)" in r.getMessage()]
        assert hits and hits[0].levelname == "WARNING" and "/reset-password?token=" in hits[0].getMessage()
    finally:
        await db.delete(u); await db.commit()


async def _ok(*a, **k):
    return "e1"


def test_startup_warning_for_resend_test_sender(monkeypatch, caplog):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "onboarding@" + "resend.dev")
    caplog.set_level(logging.WARNING, logger="autobuy.integrations.resend")
    resend.check_startup_config()
    assert any("account owner" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


# --------------------------------------------------------------------------- production boot guard

@pytest.mark.parametrize("field, value, names", [
    ("resend_api_key", "", "RESEND_API_KEY"),
    ("resend_api_key", "your_resend_api_key_here", "RESEND_API_KEY"),
    ("resend_api_key", "re_CHANGE_ME", "RESEND_API_KEY"),
    ("resend_from_email", "", "RESEND_FROM_EMAIL is empty"),
    ("resend_from_email", "onboarding@" + "resend.dev", "RESEND_FROM_EMAIL uses resend"),
    ("resend_from_email", "noreply@" + "autobuy.platform", "RESEND_FROM_EMAIL uses autobuy"),
    ("app_base_url", "", "APP_BASE_URL is empty"),
    ("app_base_url", "http://app.example.test", "APP_BASE_URL must start with https://"),
    ("app_base_url", "https://localhost:5173", "APP_BASE_URL points at localhost"),
    ("app_base_url", "https://127.0.0.1:5173", "APP_BASE_URL points at localhost/127.0.0.1"),
    ("log_reset_links", "1", "LOG_RESET_LINKS=1"),
])
def test_boot_guard_rejects_each_bad_production_value(field, value, names):
    s = Settings(**{**GOOD, field: value})
    with pytest.raises(RuntimeError, match=re.escape(names)):
        enforce_production_secrets(s)


def test_blank_compose_values_do_not_break_config(monkeypatch):
    """docker compose passes unset variables as empty strings."""
    for var in ("APP_NAME", "LOG_RESET_LINKS", "RESEND_REPLY_TO", "SUPPORT_EMAIL"):
        monkeypatch.setenv(var, "")
    s = Settings(_env_file=None)
    assert s.app_name == "Minas AutoBuy" and s.reset_links_logged is False
    assert s.resend_reply_to == "" and s.support_email == ""


def test_boot_guard_accepts_good_config_and_ignores_dev():
    enforce_production_secrets(Settings(**GOOD))  # no raise
    assert email_config_problems(Settings(**GOOD)) == []
    enforce_production_secrets(Settings(**{**GOOD, "api_env": "development", "resend_api_key": ""}))  # dev: no guard


# --------------------------------------------------------------------------- branding, templates, hardcoding

def test_app_name_drives_subjects_and_templates(monkeypatch):
    monkeypatch.setattr(settings, "app_name", "Acme Coins")
    monkeypatch.setattr(settings, "support_email", "help@example.test")
    for name in ("welcome", "purchase_summary", "first_purchase", "milestone"):
        html, text = render(name, name="Jo", rows=[], total_usdt="0.00", count=0, failed=0,
                            crypto="BTC", amount_crypto="0.1", cost_usdt="10", exchange_rate="1", order_id="o", milestone="1,000")
        assert "AutoBuy" not in html and "AutoBuy" not in text, name
        assert "Acme Coins" in html
        assert 'href="#"' not in html, "no placeholder links in any email"
    assert "help@example.test" in render("welcome", name="Jo")[1]


def test_no_hardcoded_email_domains_under_api():
    root = pathlib.Path(__file__).resolve().parents[2] / "api"
    offenders = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for needle in ("minastech.ca", "autobuy.platform", "resend.dev"):
            if needle in text:
                offenders.append(f"{p.relative_to(root.parent)}: {needle}")
    assert offenders == [], offenders


# --------------------------------------------------------------------------- password changed notice

@pytest.fixture
async def account(db):
    u = User(email=f"pwnotice-{uuid.uuid4().hex[:8]}@example.com", password_hash=hash_password("Old-Passw0rd!"))
    db.add(u); await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh); await db.commit()


async def test_password_changed_email_after_reset_and_after_change(client, account, monkeypatch):
    monkeypatch.setattr(settings, "app_name", "Acme Coins")
    monkeypatch.setattr(settings, "support_email", "help@example.test")
    sent = []

    async def fake_send(to_email, subject, html_content, from_email=None, **kw):
        sent.append((to_email, subject, kw.get("text_content", ""))); return "e1"
    monkeypatch.setattr(resend, "send_email", fake_send)
    async with client as c:
        # 1. change-password (token minted before any change is still valid)
        assert (await c.post("/api/auth/change-password", headers=auth_headers(account),
                             json={"current_password": "Old-Passw0rd!", "new_password": "New-Passw0rd!"})).status_code == 204
        # 2. reset via a token bound to the *current* hash
        from api.database import AsyncSessionLocal
        async with AsyncSessionLocal() as s2:
            fresh = (await s2.execute(select(User).where(User.id == account.id))).scalar_one()
            token = auth_mod.make_password_reset_token(fresh)
        assert (await c.post("/api/auth/reset-password", json={"token": token, "new_password": "Third-Passw0rd!"})).status_code == 204
    notices = [s for s in sent if s[1] == "Your Acme Coins password was changed"]
    assert len(notices) == 2 and all(s[0] == account.email for s in notices)
    for _, _, text in notices:
        assert re.search(r"When: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", text)
        assert "From IP address: 127.0.0.1" in text
        assert "contact help@example.test immediately" in text


async def test_password_change_survives_email_failure(client, account, monkeypatch, caplog):
    async def boom(*a, **k):
        raise ResendError("Resend 500: down", status=500)
    monkeypatch.setattr(resend, "send_email", boom)
    caplog.set_level(logging.ERROR, logger="autobuy.auth")
    async with client as c:
        r = await c.post("/api/auth/change-password", headers=auth_headers(account),
                         json={"current_password": "Old-Passw0rd!", "new_password": "New-Passw0rd!"})
        assert r.status_code == 204
        assert (await c.post("/api/auth/login", json={"email": account.email, "password": "New-Passw0rd!"})).status_code == 200
    assert any("password changed notice not delivered" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)
