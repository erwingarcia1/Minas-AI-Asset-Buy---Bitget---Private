"""Who sees Guardian (v0.9.2): the flag, the allowlist and the 404 dependency.

The decision lives in api/services/guardian_access.py and is made server-side:
flag off hides Guardian from everyone, flag on with an empty allowlist shows it
to everyone, and an allowlist is compared lower-case. A user who cannot see it
gets 404 "Not found" from the router dependency, the flag is checked before
the credentials (flag off: 404 for an anonymous caller too, nobody is asked to
sign in), and the profile reports the same decision for the UI.
"""
import uuid

import httpx
from fastapi import Depends, FastAPI

from api.config import settings
from api.models import User
from api.routes.user import get_current_user
from api.services.guardian_access import guardian_visible, require_guardian_enabled, require_guardian_flag
from tests.api.conftest import auth_headers


def someone(email="alice@example.com") -> User:
    return User(id=uuid.uuid4(), email=email, password_hash="x")


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_flag_off_hides_guardian_from_everyone(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", False)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "alice@example.com")
    assert guardian_visible(someone()) is False
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
    assert guardian_visible(someone()) is False


def test_flag_on_with_an_empty_allowlist_shows_it_to_everyone(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
    assert guardian_visible(someone()) is True
    assert guardian_visible(someone("anyone@example.com")) is True


def test_allowlist_membership_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", " Alice@Example.COM ,bob@example.com")
    assert guardian_visible(someone("alice@example.com")) is True
    assert guardian_visible(someone("ALICE@example.com")) is True
    assert guardian_visible(someone("bob@example.com")) is True
    assert guardian_visible(someone("carol@example.com")) is False
    assert guardian_visible(None) is False, "no user, no Guardian"


async def test_the_dependency_answers_404_not_found_when_hidden(monkeypatch):
    app = FastAPI()
    user = someone()
    app.dependency_overrides[get_current_user] = lambda: user

    @app.get("/g", dependencies=[Depends(require_guardian_enabled)])
    async def guarded():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        monkeypatch.setattr(settings, "guardian_enabled", False)
        r = await c.get("/g")
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}
        monkeypatch.setattr(settings, "guardian_enabled", True)
        monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "someone-else@example.com")
        r = await c.get("/g")
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}, "not on the allowlist: the same 404"
        monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
        r = await c.get("/g")
        assert r.status_code == 200 and r.json() == {"ok": True}


async def test_the_flag_is_checked_before_the_credentials(monkeypatch):
    """Flag off: 404 for everyone and no request to sign in, so a probe cannot
    tell a feature that is off from a path that does not exist. Flag on: an
    anonymous caller gets the usual 401. Both the router-level order and the
    dependency's own parameter order give this."""
    app = FastAPI()

    @app.get("/g", dependencies=[Depends(require_guardian_flag), Depends(require_guardian_enabled)])
    async def guarded():
        return {"ok": True}

    @app.get("/h", dependencies=[Depends(require_guardian_enabled)])
    async def guarded_by_the_dependency_alone():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        monkeypatch.setattr(settings, "guardian_enabled", False)
        for path in ("/g", "/h"):
            r = await c.get(path)
            assert (r.status_code, r.json()) == (404, {"detail": "Not found"}), (path, r.text)
            assert "www-authenticate" not in r.headers, path
        monkeypatch.setattr(settings, "guardian_enabled", True)
        monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
        for path in ("/g", "/h"):
            r = await c.get(path)
            assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer", (path, r.text)


async def test_the_dependency_returns_the_user_when_visible(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "")
    user = someone()
    assert await require_guardian_enabled(current_user=user) is user


async def test_the_profile_reports_the_same_decision(user, monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", False)
    async with fresh_client() as c:
        body = (await c.get("/api/user/profile", headers=auth_headers(user))).json()
    assert body["guardian_enabled"] is False and body["mfa_enabled"] is False
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", "nobody-else@example.com")
    async with fresh_client() as c:
        assert (await c.get("/api/user/profile", headers=auth_headers(user))).json()["guardian_enabled"] is False
    monkeypatch.setattr(settings, "guardian_allowed_emails_csv", f"other@example.com,{user.email.upper()}")
    async with fresh_client() as c:
        assert (await c.get("/api/user/profile", headers=auth_headers(user))).json()["guardian_enabled"] is True
