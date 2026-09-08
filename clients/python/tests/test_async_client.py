from __future__ import annotations

import asyncio

import pytest

from autobuy_client import AsyncAutoBuyClient, AuthenticationError, RateLimitError
from tests.conftest import TOKENS, body_of, json_response


async def test_async_register_login_profile(server):
    (server
     .on("POST", "/api/auth/register", lambda r: json_response({"id": "u1", "email": "a@b.co", "kyc_status": "pending", "created_at": "x"}, 201))
     .on("POST", "/api/auth/login", lambda r: json_response(TOKENS))
     .on("GET", "/api/user/profile", lambda r: json_response({"id": "u1", "email": "a@b.co", "kyc_status": "pending", "created_at": "x"})))
    async with AsyncAutoBuyClient("http://api.test", transport=server.async_transport) as client:
        assert (await client.register("a@b.co", "pw"))["id"] == "u1"
        await client.login("a@b.co", "pw")
        assert (await client.me())["email"] == "a@b.co"
        assert server.calls[2].headers["authorization"] == "Bearer acc-1"


async def test_async_concurrent_401s_share_one_refresh(server):
    def stats(r):
        if r.headers.get("authorization") == "Bearer acc-new":
            return json_response({"total_invested_usdt": 1, "transaction_count": 0, "active_purchases": 0, "kyc_status": "verified"})
        return json_response({"detail": "expired"}, 401)

    server.on("GET", "/api/dashboard/stats", stats)
    server.on("POST", "/api/auth/refresh", lambda r: json_response(
        {"access_token": "acc-new", "refresh_token": "ref-new", "token_type": "bearer", "expires_in": 1800}))
    client = AsyncAutoBuyClient("http://api.test", transport=server.async_transport,
                                tokens={"access_token": "acc-old", "refresh_token": "ref-old"})
    a, b = await asyncio.gather(client.dashboard.quick_stats(), client.dashboard.quick_stats())
    assert a["total_invested_usdt"] == b["total_invested_usdt"] == 1
    assert sum(1 for c in server.calls if c.url.path == "/api/auth/refresh") == 1
    assert client.tokens["refresh_token"] == "ref-new"
    await client.aclose()


async def test_async_failed_refresh_raises_and_clears(server):
    server.on("GET", "/api/user/profile", lambda r: json_response({"detail": "expired"}, 401))
    server.on("POST", "/api/auth/refresh", lambda r: json_response({"detail": "revoked"}, 401))
    client = AsyncAutoBuyClient("http://api.test", transport=server.async_transport, tokens=TOKENS)
    with pytest.raises(AuthenticationError):
        await client.me()
    assert client.tokens is None


async def test_async_rate_limit_and_query(server):
    server.on("GET", "/api/transactions/", lambda r: json_response({"detail": "slow down"}, 429, {"retry-after": "7"}))
    client = AsyncAutoBuyClient("http://api.test", transport=server.async_transport, tokens=TOKENS)
    with pytest.raises(RateLimitError) as ei:
        await client.transactions.list_transactions(limit=5)
    assert ei.value.retry_after == 7
    assert str(server.calls[0].url) == "http://api.test/api/transactions/?limit=5"


async def test_async_logout(server):
    server.on("POST", "/api/auth/logout", lambda r: json_response({"detail": "ok"}))
    client = AsyncAutoBuyClient("http://api.test", transport=server.async_transport, tokens=TOKENS)
    await client.logout()
    assert body_of(server.calls[0]) == {"refresh_token": "ref-1"}
    assert client.tokens is None
