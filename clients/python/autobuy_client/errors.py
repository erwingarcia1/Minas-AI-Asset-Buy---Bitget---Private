"""Exception hierarchy for the AutoBuy client."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class AutoBuyError(Exception):
    """Base class for all client errors."""


class ApiError(AutoBuyError):
    """Any non-2xx response.

    ``detail`` is the parsed JSON ``detail`` field (FastAPI convention) when the
    body was JSON, otherwise the raw text. ``body`` is always the raw text.
    """

    def __init__(self, status: int, method: str, path: str, detail: Any, body: str,
                 headers: Dict[str, str]) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail
        self.body = body
        self.headers = headers
        shown = detail if isinstance(detail, str) else body[:200]
        super().__init__(f"{method} {path} -> {status}: {shown}")


class AuthenticationError(ApiError):
    """401 that survived a refresh attempt (or no refresh token was available)."""


class RateLimitError(ApiError):
    """429. ``retry_after`` is the server's ``Retry-After`` in seconds (None if absent)."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        ra = self.headers.get("retry-after")
        try:
            self.retry_after: Optional[int] = int(ra) if ra not in (None, "") else None
        except ValueError:
            self.retry_after = None


class ValidationError(ApiError):
    """422. ``errors`` are the individual pydantic validation failures."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        d = self.detail
        if isinstance(d, list):
            self.errors: List[Dict[str, Any]] = d
        elif isinstance(d, dict) and isinstance(d.get("detail"), list):
            self.errors = d["detail"]
        else:
            self.errors = []
