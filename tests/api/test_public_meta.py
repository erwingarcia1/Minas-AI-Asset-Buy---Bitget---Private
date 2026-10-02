"""GET /api/public/meta: anonymous, read-only, cacheable, only the four facts."""
import pytest

from api.config import settings
from api.utils.exchange_networks import SUPPORTED_CRYPTOS


async def test_meta_is_anonymous_and_carries_only_the_four_facts(client, monkeypatch):
    monkeypatch.setattr(settings, "public_server_ip", "203.0.113.7")
    monkeypatch.setattr(settings, "support_email", "support@example.com")
    async with client as c:
        r = await c.get("/api/public/meta")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=300"
    body = r.json()
    assert set(body) == {"coins", "coin_categories", "accumulate_only", "exchanges", "support_email", "server_ip"}
    assert [g["category"] for g in body["coin_categories"]] == ["main", "stable", "meme"] and body["accumulate_only"] == ["DOGE", "PEPE", "SHIB"]
    assert body["coins"] == list(SUPPORTED_CRYPTOS) and "BTC" in body["coins"]
    assert body["exchanges"] == ["bitget"]
    assert body == {**body, "support_email": "support@example.com", "server_ip": "203.0.113.7"}
    text = r.text.lower()
    for secret in ("secret", "token", "api_key", "password", "database", "redis"):
        assert secret not in text


async def test_meta_defaults_to_empty_strings_when_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "public_server_ip", "")
    monkeypatch.setattr(settings, "support_email", "")
    async with client as c:
        r = await c.get("/api/public/meta")
    assert r.status_code == 200 and r.json()["server_ip"] == "" and r.json()["support_email"] == ""


async def test_meta_is_rate_limited(client):
    from api.services.rate_limit import limiter
    limiter.reset()
    limiter.enabled = True    # off under tests by default, as in test_rate_limit.py
    try:
        async with client as c:
            codes = [(await c.get("/api/public/meta")).status_code for _ in range(61)]
    finally:
        limiter.enabled = False
        limiter.reset()
    assert codes[:60] == [200] * 60 and codes[60] == 429
