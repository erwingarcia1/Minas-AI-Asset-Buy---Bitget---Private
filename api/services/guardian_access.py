"""Who can see Guardian: the feature flag and the optional allowlist (v0.9.2).

``GUARDIAN_ENABLED`` switches the monitor on for the deployment;
``GUARDIAN_ALLOWED_EMAILS`` (comma-separated, compared lower-case; empty means
everyone) narrows it to named customers during the private roll-out. Both are
read from ``api.config.settings``; the owner sets them in ``.env`` and nothing
here changes them.

The decision is made server-side and fails closed: a user who cannot see
Guardian gets 404 from every Guardian path, as if the feature did not exist,
and while the flag is off so does everyone, signed in or not: the flag is
checked before any credential, so a probe cannot tell a feature that is
switched off from a path that does not exist. The UI only explains what the
server already decided.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from api.config import settings
from api.models import User
from api.routes.user import get_current_user

__all__ = ["NOT_FOUND", "guardian_visible", "require_guardian_enabled", "require_guardian_flag"]

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


def require_guardian_flag() -> None:
    """The first gate of every Guardian route, before authentication: while
    ``GUARDIAN_ENABLED`` is off every path is 404 "Not found" for everyone,
    anonymous callers included, so nobody is asked to sign in for a feature
    that is switched off and a probe learns nothing from the answer."""
    if not settings.guardian_enabled:
        raise HTTPException(status_code=404, detail=NOT_FOUND)


async def require_guardian_enabled(_flag: None = Depends(require_guardian_flag),
                                   current_user: User = Depends(get_current_user)) -> User:
    """Router-level dependency for every Guardian route: the authenticated
    user when Guardian is visible to them, otherwise 404 "Not found". The
    flag is resolved before the credentials (`require_guardian_flag` is the
    first parameter, and the first router dependency), so with the feature
    off the answer is 404 before anyone is asked to sign in; with it on, an
    anonymous caller gets the usual 401 and a user outside the allowlist the
    same 404."""
    if not guardian_visible(current_user):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return current_user
