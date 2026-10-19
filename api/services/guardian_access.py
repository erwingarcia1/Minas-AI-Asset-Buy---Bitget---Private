"""Who can see Guardian: the feature flag and the optional allowlist (v0.9.2).

``GUARDIAN_ENABLED`` switches the monitor on for the deployment;
``GUARDIAN_ALLOWED_EMAILS`` (comma-separated, compared lower-case; empty means
everyone) narrows it to named customers during the private roll-out. Both are
read from ``api.config.settings``; the owner sets them in ``.env`` and nothing
here changes them.

The decision is made server-side and fails closed: a user who cannot see
Guardian gets 404 from every Guardian path, as if the feature did not exist.
The UI only explains what the server already decided.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from api.config import settings
from api.models import User
from api.routes.user import get_current_user

__all__ = ["NOT_FOUND", "guardian_visible", "require_guardian_enabled"]

NOT_FOUND = "Not found"


def guardian_visible(user) -> bool:
    """May this user see Guardian? The flag must be on and, when an allowlist
    is configured, the user's email (compared lower-case) must be on it."""
    if not settings.guardian_enabled:
        return False
    allowed = settings.guardian_allowed_emails
    if not allowed:
        return True
    email = (getattr(user, "email", None) or "").strip().lower()
    return bool(email) and email in allowed


async def require_guardian_enabled(current_user: User = Depends(get_current_user)) -> User:
    """Router-level dependency for every Guardian route: the authenticated
    user when Guardian is visible to them, otherwise 404 "Not found"."""
    if not guardian_visible(current_user):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return current_user
