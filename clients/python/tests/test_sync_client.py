from __future__ import annotations

import pytest

from autobuy_client import (ApiError, AuthenticationError, AutoBuyClient, RateLimitError,
                            ValidationError)
from tests.conftest import TOKENS, body_of, json_response


def test_register_login_profile_attaches_bearer(server):
    (server
     .on("POST", "/api/auth/register", lambda r: json_response(
         {"id": "u1", "email": body_of(r)["email"], "kyc_status": "pending", "created_at": "2026-09-08T00:00:00Z"}, 201))
     .on("POST", "/api/auth/login", lambda r: json_response(TOKENS))
     .on("GET", "/api/user/profile", lambda r: json_response(
         {"id": "u1", "email": "a@b.co", "kyc_status": "pending", "created_at": "x"})
         if r.headers.get("authorization") == "Bearer acc-1" else json_response({"detail": "nope"}, 401)))
    changes = []
    client = AutoBuyClient("http://api.test/", transport=server.transport, on_tokens_changed=changes.append)

    user = client.register("a@b.co", "Str0ng!Passw0rd")
    assert user["email"] == "a@b.co"
    assert "authorization" not in server.calls[0].headers

    pair = client.login("a@b.co", "Str0ng!Passw0rd")
    assert pair["access_token"] == "acc-1"
    assert client.tokens == TOKENS
    assert changes == [TOKENS]

    me = client.me()
    assert me["id"] == "u1"
    assert server.calls[2].headers["authorization"] == "Bearer acc-1"


def test_create_purchase_sends_json_body(server):
    server.on("POST", "/api/purchases/", lambda r: json_response(
        {"id": "p1", **body_of(r), "is_active": True, "next_purchase_date": "2026-09-15T00:00:00Z",
         "created_at": "x", "exchange": "binance"}, 201))
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    p = client.create_purchase({"crypto": "BTC", "amount_usdt": 50, "frequency": "weekly"})
    assert p["id"] == "p1" and p["crypto"] == "BTC"
    assert server.calls[0].headers["content-type"] == "application/json"
    assert body_of(server.calls[0]) == {"crypto": "BTC", "amount_usdt": 50, "frequency": "weekly"}


def test_list_transactions_query_params_skip_none(server):
    server.on("GET", "/api/transactions/", lambda r: json_response({"transactions": [], "total": 0, "limit": 10, "offset": 20}))
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    res = client.transactions.list_transactions(limit=10, offset=20)
    assert res["total"] == 0
    assert str(server.calls[0].url) == "http://api.test/api/transactions/?limit=10&offset=20"


def test_path_params_are_url_encoded(server):
    server.on("GET", "/api/transactions/crypto/BTC/X", lambda r: json_response([]))  # httpx decodes url.path
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    client.transactions.transactions_by_crypto("BTC/X")
    assert server.calls[0].url.raw_path == b"/api/transactions/crypto/BTC%2FX"


def test_401_refreshes_once_and_retries(server):
    def stats(r):
        if r.headers.get("authorization") == "Bearer acc-new":
            return json_response({"total_invested_usdt": 1, "transaction_count": 0, "active_purchases": 0, "kyc_status": "verified"})
        return json_response({"detail": "expired"}, 401)

    def refresh(r):
        assert body_of(r) == {"refresh_token": "ref-old"}
        return json_response({"access_token": "acc-new", "refresh_token": "ref-new", "token_type": "bearer", "expires_in": 1800})

    server.on("GET", "/api/dashboard/stats", stats).on("POST", "/api/auth/refresh", refresh)
    changes = []
    client = AutoBuyClient("http://api.test", transport=server.transport,
                           tokens={"access_token": "acc-old", "refresh_token": "ref-old"}, on_tokens_changed=changes.append)
    assert client.dashboard.quick_stats()["total_invested_usdt"] == 1
    paths = [c.url.path for c in server.calls]
    assert paths == ["/api/dashboard/stats", "/api/auth/refresh", "/api/dashboard/stats"]
    assert client.tokens["refresh_token"] == "ref-new"
    assert len(changes) == 1


def test_failed_refresh_clears_tokens_and_raises(server):
    server.on("GET", "/api/user/profile", lambda r: json_response({"detail": "expired"}, 401))
    server.on("POST", "/api/auth/refresh", lambda r: json_response({"detail": "revoked"}, 401))
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    with pytest.raises(AuthenticationError) as ei:
        client.me()
    assert ei.value.status == 401
    assert client.tokens is None


def test_429_raises_rate_limit_error_with_retry_after(server):
    server.on("POST", "/api/auth/login", lambda r: json_response(
        {"detail": "Rate limit exceeded: 5 per 1 minute"}, 429, {"retry-after": "42"}))
    client = AutoBuyClient("http://api.test", transport=server.transport)
    with pytest.raises(RateLimitError) as ei:
        client.login("a@b.co", "pw")
    assert ei.value.retry_after == 42 and ei.value.status == 429


def test_422_raises_validation_error_with_field_errors(server):
    server.on("POST", "/api/purchases/", lambda r: json_response(
        {"detail": [{"loc": ["body", "amount_usdt"], "msg": "ensure this value is >= 10", "type": "value_error"}]}, 422))
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    with pytest.raises(ValidationError) as ei:
        client.create_purchase({"crypto": "BTC", "amount_usdt": 1, "frequency": "weekly"})
    assert ei.value.errors[0]["loc"][1] == "amount_usdt"


def test_other_errors_raise_api_error(server):
    server.on("GET", "/api/purchases/nope", lambda r: json_response({"detail": "Purchase not found"}, 404))
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    with pytest.raises(ApiError) as ei:
        client.purchases.get_purchase("nope")
    assert ei.value.detail == "Purchase not found"
    assert not isinstance(ei.value, RateLimitError)


def test_logout_revokes_and_clears(server):
    server.on("POST", "/api/auth/logout", lambda r: json_response({"detail": "ok"}))
    client = AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS)
    client.logout()
    assert body_of(server.calls[0]) == {"refresh_token": "ref-1"}
    assert client.tokens is None


def test_health_unauthenticated_and_empty_bodies(server):
    server.on("GET", "/health", lambda r: json_response({"status": "healthy"}))
    server.on("DELETE", "/api/purchases/p1", lambda r: __import__("httpx").Response(204))
    with AutoBuyClient("http://api.test", transport=server.transport, tokens=TOKENS) as client:
        assert client.system.health_check()["status"] == "healthy"
        assert "authorization" not in server.calls[0].headers
        assert client.purchases.delete_purchase("p1") is None
