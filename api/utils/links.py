"""Links that appear in emails, built only from configuration.

``build_app_link`` never looks at the incoming request (Host, Origin,
X-Forwarded-Host), so a spoofed header can never redirect a password reset
or verification link. Base URL resolution:

1. ``APP_BASE_URL`` (trailing slash stripped) — the only source in production;
   the production boot guard refuses to start without it, so the fallbacks
   below can only run in development.
2. the first ``ALLOWED_ORIGINS`` entry
3. ``http://localhost:5173`` (the Vite dev server in ``frontend/``)
"""
from __future__ import annotations

from urllib.parse import urlencode

from api.config import settings

__all__ = ["app_base_url", "build_app_link"]


def app_base_url() -> str:
    base = (settings.app_base_url or "").strip().rstrip("/")
    if base:
        return base
    if settings.api_env == "production":
        raise RuntimeError("APP_BASE_URL is not configured; refusing to build an email link in production")
    origins = settings.allowed_origins
    return (origins[0] if origins else "http://localhost:5173").rstrip("/")


def build_app_link(path: str, **query) -> str:
    """``{APP_BASE_URL}{path}?{query}``; query values that are None are dropped."""
    if not path.startswith("/"):
        path = "/" + path
    qs = urlencode({k: v for k, v in query.items() if v is not None})
    return f"{app_base_url()}{path}" + (f"?{qs}" if qs else "")
