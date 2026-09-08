"""Hand-written HTTP transports (sync + async) for the AutoBuy Python client.

Responsibilities: base URL + JSON encoding, bearer auth, transparent refresh on
401 (single-flight for the async transport), typed errors. Built on ``httpx``;
a custom ``transport`` (e.g. ``httpx.MockTransport``) can be injected for tests.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import quote

import httpx

from .errors import ApiError, AuthenticationError, RateLimitError, ValidationError

TokenPair = Dict[str, Any]  # {"access_token", "refresh_token", "token_type", "expires_in"}
TokensCallback = Callable[[Optional[TokenPair]], None]

DEFAULT_REFRESH_PATH = "/api/auth/refresh"
DEFAULT_TIMEOUT = 30.0


def _q(value: Any) -> str:
    """URL-encode a path parameter."""
    return quote(str(value), safe="")


def _clean_query(query: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not query:
        return None
    cleaned = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in query.items() if v is not None}
    return cleaned or None


def _parse_body(res: httpx.Response) -> Any:
    if res.status_code == 204 or not res.content:
        return None
    if "application/json" in res.headers.get("content-type", ""):
        return res.json()
    return res.text


def _raise_for(res: httpx.Response, method: str, path: str) -> None:
    text = res.text
    detail: Any = text
    try:
        parsed = res.json()
        detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
    except ValueError:
        pass
    headers = {k.lower(): v for k, v in res.headers.items()}
    args = (res.status_code, method, path, detail, text, headers)
    if res.status_code == 429:
        raise RateLimitError(*args)
    if res.status_code == 422:
        raise ValidationError(*args)
    if res.status_code == 401:
        raise AuthenticationError(*args)
    raise ApiError(*args)


class _BaseTransport:
    def __init__(self, base_url: str, *, tokens: Optional[TokenPair] = None,
                 on_tokens_changed: Optional[TokensCallback] = None,
                 headers: Optional[Mapping[str, str]] = None,
                 refresh_path: str = DEFAULT_REFRESH_PATH) -> None:
        self.base_url = base_url.rstrip("/")
        self._tokens: Optional[TokenPair] = dict(tokens) if tokens else None
        self._on_tokens_changed = on_tokens_changed
        self._extra_headers = dict(headers or {})
        self._refresh_path = refresh_path

    # -- tokens ---------------------------------------------------------------
    def get_tokens(self) -> Optional[TokenPair]:
        return self._tokens

    def set_tokens(self, tokens: Optional[TokenPair]) -> None:
        self._tokens = dict(tokens) if tokens else None
        if self._on_tokens_changed:
            self._on_tokens_changed(self._tokens)

    @property
    def _refresh_token(self) -> Optional[str]:
        return (self._tokens or {}).get("refresh_token")

    # -- request building -----------------------------------------------------
    def _prepare(self, method: str, path: str, *, body: Any, query: Optional[Mapping[str, Any]],
                 auth: bool, headers: Optional[Mapping[str, str]]) -> Dict[str, Any]:
        h: Dict[str, str] = {"Accept": "application/json", **self._extra_headers, **dict(headers or {})}
        if auth and self._tokens and self._tokens.get("access_token"):
            h["Authorization"] = f"Bearer {self._tokens['access_token']}"
        kwargs: Dict[str, Any] = {"method": method, "url": self.base_url + (path if path.startswith("/") else "/" + path),
                                  "headers": h, "params": _clean_query(query)}
        if body is not None:
            kwargs["json"] = body
        return kwargs


class SyncTransport(_BaseTransport):
    """Blocking transport on ``httpx.Client``."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT,
                 transport: Optional[httpx.BaseTransport] = None, **kw: Any) -> None:
        super().__init__(base_url, **kw)
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SyncTransport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def refresh(self) -> TokenPair:
        """Exchange the refresh token for a new pair (thread-safe)."""
        with self._lock:
            rt = self._refresh_token
            if not rt:
                raise AuthenticationError(401, "POST", self._refresh_path, "No refresh token available", "", {})
            pair = self.request("POST", self._refresh_path, body={"refresh_token": rt}, auth=False)
            self.set_tokens(pair)
            return pair

    def request(self, method: str, path: str, *, body: Any = None, query: Optional[Mapping[str, Any]] = None,
                auth: bool = True, headers: Optional[Mapping[str, str]] = None) -> Any:
        res = self._client.request(**self._prepare(method, path, body=body, query=query, auth=auth, headers=headers))
        if res.status_code == 401 and auth and self._refresh_token:
            try:
                self.refresh()
            except ApiError:
                self.set_tokens(None)
                _raise_for(res, method, path)
            res = self._client.request(**self._prepare(method, path, body=body, query=query, auth=auth, headers=headers))
        if res.is_error:
            _raise_for(res, method, path)
        return _parse_body(res)


class AsyncTransport(_BaseTransport):
    """Non-blocking transport on ``httpx.AsyncClient``."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT,
                 transport: Optional[httpx.AsyncBaseTransport] = None, **kw: Any) -> None:
        super().__init__(base_url, **kw)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._refreshing: Optional["asyncio.Task[TokenPair]"] = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncTransport":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def refresh(self) -> TokenPair:
        """Exchange the refresh token for a new pair. Concurrent callers share one in-flight refresh."""
        if self._refreshing is None or self._refreshing.done():
            self._refreshing = asyncio.ensure_future(self._do_refresh())
        return await self._refreshing

    async def _do_refresh(self) -> TokenPair:
        rt = self._refresh_token
        if not rt:
            raise AuthenticationError(401, "POST", self._refresh_path, "No refresh token available", "", {})
        pair = await self.request("POST", self._refresh_path, body={"refresh_token": rt}, auth=False)
        self.set_tokens(pair)
        return pair

    async def request(self, method: str, path: str, *, body: Any = None, query: Optional[Mapping[str, Any]] = None,
                      auth: bool = True, headers: Optional[Mapping[str, str]] = None) -> Any:
        res = await self._client.request(**self._prepare(method, path, body=body, query=query, auth=auth, headers=headers))
        if res.status_code == 401 and auth and self._refresh_token:
            try:
                await self.refresh()
            except ApiError:
                self.set_tokens(None)
                _raise_for(res, method, path)
            res = await self._client.request(**self._prepare(method, path, body=body, query=query, auth=auth, headers=headers))
        if res.is_error:
            _raise_for(res, method, path)
        return _parse_body(res)
