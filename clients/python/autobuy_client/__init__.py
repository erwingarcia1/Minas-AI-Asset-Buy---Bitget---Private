"""AutoBuy Crypto API — Python client.

    from autobuy_client import AutoBuyClient

    client = AutoBuyClient("http://localhost:8000")
    client.login("me@example.com", "Str0ng!Passw0rd")
    purchase = client.auto_buy.create_purchase({"crypto_symbol": "BTC", "amount_usd": 50, "frequency": "weekly",
                                               "wallet_address": "bc1q...", "blockchain_network": "mainnet",
                                               "exchange_account_id": "<api key id>", "purchase_type": "autobuy"})

An ``AsyncAutoBuyClient`` with the same surface is available for asyncio code.
Resource groups and types are generated from the API's OpenAPI spec.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Union

import httpx

from . import types
from ._http import AsyncTransport, SyncTransport, TokenPair, TokensCallback
from .api import Api, AsyncApi
from .errors import ApiError, AuthenticationError, AutoBuyError, RateLimitError, ValidationError
from .types import (MfaLoginRequest, MfaRequiredResponse, PurchaseCreateRequest, PurchaseCreatedOut, TokenResponse,
                    UserOut, UserProfile)

__version__ = "0.5.0"
__all__ = [
    "AutoBuyClient", "AsyncAutoBuyClient", "Api", "AsyncApi", "types",
    "ApiError", "AuthenticationError", "AutoBuyError", "RateLimitError", "ValidationError",
    "TokenPair", "__version__",
]


def _mfa_required(result: Any) -> bool:
    """True when ``POST /api/auth/login`` answered 202: a two-factor challenge, not a token pair."""
    return isinstance(result, dict) and bool(result.get("mfa_required"))


def _mfa_body(mfa_token: str, code: Optional[str], recovery_code: Optional[str]) -> MfaLoginRequest:
    if code is None and recovery_code is None:
        raise ValueError("login_mfa needs either code or recovery_code")
    body: MfaLoginRequest = {"mfa_token": mfa_token}
    if code is not None:
        body["code"] = code
    if recovery_code is not None:
        body["recovery_code"] = recovery_code
    return body


class AutoBuyClient:
    """Blocking client: resource groups from the OpenAPI spec plus token-lifecycle helpers."""

    def __init__(self, base_url: str = "http://localhost:8000", *, tokens: Optional[TokenPair] = None,
                 on_tokens_changed: Optional[TokensCallback] = None, headers: Optional[Mapping[str, str]] = None,
                 timeout: float = 30.0, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.transport = SyncTransport(base_url, tokens=tokens, on_tokens_changed=on_tokens_changed,
                                       headers=headers, timeout=timeout, transport=transport)
        api = Api(self.transport)
        self.system = api.system
        self.auth = api.auth
        self.user = api.user
        self.exchange = api.exchange
        self.transactions = api.transactions
        self.dashboard = api.dashboard
        self.integrations = api.integrations
        self.reports = api.reports
        self.notifications = api.notifications
        self.admin = api.admin
        self.analytics = api.analytics
        self.kyc = api.kyc
        self.webhooks = api.webhooks
        #: Auto-Buy purchases delivered to validated wallets (``/api/v2/purchases``).
        self.auto_buy = api.v2
        self.wallets = api.wallets

    # -- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "AutoBuyClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- auth helpers ------------------------------------------------------------
    @property
    def tokens(self) -> Optional[TokenPair]:
        """Current token pair (None when logged out)."""
        return self.transport.get_tokens()

    def set_tokens(self, tokens: Optional[TokenPair]) -> None:
        """Install a token pair obtained elsewhere (e.g. restored from storage)."""
        self.transport.set_tokens(tokens)

    def register(self, email: str, password: str) -> UserOut:
        """``POST /api/auth/register`` — create an account. Does not log in."""
        return self.auth.register({"email": email, "password": password})

    def login(self, email: str, password: str) -> Union[TokenResponse, MfaRequiredResponse]:
        """``POST /api/auth/login`` — authenticate and store the resulting tokens.

        An account with two-factor authentication on answers ``202`` with
        ``{"mfa_required": True, "mfa_token": ..., "expires_in": ...}`` instead of a token
        pair, and nothing is stored. Check ``result.get("mfa_required")`` and finish the
        sign-in with :meth:`login_mfa` within ``expires_in`` seconds.
        """
        result = self.auth.login({"email": email, "password": password})
        if _mfa_required(result):
            return result
        self.transport.set_tokens(result)
        return result

    def login_mfa(self, mfa_token: str, code: Optional[str] = None,
                  recovery_code: Optional[str] = None) -> TokenResponse:
        """``POST /api/auth/login/mfa`` — finish a two-factor sign-in and store the resulting tokens.

        ``mfa_token`` is the one returned by :meth:`login`; pass either the authenticator
        ``code`` or a ``recovery_code``.
        """
        pair = self.auth.login_mfa(_mfa_body(mfa_token, code, recovery_code))
        self.transport.set_tokens(pair)
        return pair

    def refresh_tokens(self) -> TokenPair:
        """Force a refresh now (normally happens automatically on 401)."""
        return self.transport.refresh()

    def logout(self) -> None:
        """``POST /api/auth/logout`` — revoke the refresh cookie server-side and clear local state."""
        try:
            self.transport.request("POST", "/api/auth/logout", auth=False, headers=self.transport._csrf_headers())
        except ApiError:
            pass
        self.transport.set_tokens(None)

    def me(self) -> UserProfile:
        """``GET /api/user/profile`` — the authenticated user."""
        return self.user.get_profile()

    def create_purchase(self, body: PurchaseCreateRequest) -> PurchaseCreatedOut:
        """Convenience wrapper around ``auto_buy.create_purchase`` (``POST /api/v2/purchases``)."""
        return self.auto_buy.create_purchase(body)


class AsyncAutoBuyClient:
    """asyncio client with the same surface as :class:`AutoBuyClient`."""

    def __init__(self, base_url: str = "http://localhost:8000", *, tokens: Optional[TokenPair] = None,
                 on_tokens_changed: Optional[TokensCallback] = None, headers: Optional[Mapping[str, str]] = None,
                 timeout: float = 30.0, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.transport = AsyncTransport(base_url, tokens=tokens, on_tokens_changed=on_tokens_changed,
                                        headers=headers, timeout=timeout, transport=transport)
        api = AsyncApi(self.transport)
        self.system = api.system
        self.auth = api.auth
        self.user = api.user
        self.exchange = api.exchange
        self.transactions = api.transactions
        self.dashboard = api.dashboard
        self.integrations = api.integrations
        self.reports = api.reports
        self.notifications = api.notifications
        self.admin = api.admin
        self.analytics = api.analytics
        self.kyc = api.kyc
        self.webhooks = api.webhooks
        #: Auto-Buy purchases delivered to validated wallets (``/api/v2/purchases``).
        self.auto_buy = api.v2
        self.wallets = api.wallets

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> "AsyncAutoBuyClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @property
    def tokens(self) -> Optional[TokenPair]:
        return self.transport.get_tokens()

    def set_tokens(self, tokens: Optional[TokenPair]) -> None:
        self.transport.set_tokens(tokens)

    async def register(self, email: str, password: str) -> UserOut:
        return await self.auth.register({"email": email, "password": password})

    async def login(self, email: str, password: str) -> Union[TokenResponse, MfaRequiredResponse]:
        """See :meth:`AutoBuyClient.login`: a 202 two-factor challenge is returned, not stored."""
        result = await self.auth.login({"email": email, "password": password})
        if _mfa_required(result):
            return result
        self.transport.set_tokens(result)
        return result

    async def login_mfa(self, mfa_token: str, code: Optional[str] = None,
                        recovery_code: Optional[str] = None) -> TokenResponse:
        """See :meth:`AutoBuyClient.login_mfa`."""
        pair = await self.auth.login_mfa(_mfa_body(mfa_token, code, recovery_code))
        self.transport.set_tokens(pair)
        return pair

    async def refresh_tokens(self) -> TokenPair:
        return await self.transport.refresh()

    async def logout(self) -> None:
        try:
            await self.transport.request("POST", "/api/auth/logout", auth=False, headers=self.transport._csrf_headers())
        except ApiError:
            pass
        self.transport.set_tokens(None)

    async def me(self) -> UserProfile:
        return await self.user.get_profile()

    async def create_purchase(self, body: PurchaseCreateRequest) -> PurchaseCreatedOut:
        return await self.auto_buy.create_purchase(body)
