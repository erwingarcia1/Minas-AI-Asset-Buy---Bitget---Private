import uuid

import httpx
import pytest
from pydantic import ValidationError

from api.auth import create_access_token
from api.routes.auth import RegisterRequest
from api.routes.purchases import PurchaseCreate, validate_withdrawal_address


def make(**kw):
    base = {"crypto": "BTC", "amount_usdt": 100.0, "frequency": "weekly"}
    base.update(kw)
    return PurchaseCreate(**base)


def test_amount_bounds():
    assert make(amount_usdt=10).amount_usdt == 10
    assert make(amount_usdt=1_000_000).amount_usdt == 1_000_000
    for bad in (9.99, 0, -5, 1_000_001):
        with pytest.raises(ValidationError):
            make(amount_usdt=bad)


def test_crypto_whitelist():
    assert make(crypto="eth").crypto == "ETH"
    with pytest.raises(ValidationError, match="crypto must be one of"):
        make(crypto="SHIB")


def test_address_must_match_crypto():
    btc = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    eth = "0x52908400098527886E0F7030069857D2E4169EE7"
    assert make(crypto="BTC", withdrawal_address=btc).withdrawal_address == btc
    assert make(crypto="ETH", withdrawal_address=eth).withdrawal_address == eth
    with pytest.raises(ValidationError, match="not a valid BTC address"):
        make(crypto="BTC", withdrawal_address=eth)
    with pytest.raises(ValidationError, match="not a valid ETH address"):
        make(crypto="ETH", withdrawal_address=btc)
    # helper used by the update route
    validate_withdrawal_address("SOL", "4Nd1mYQK4YyFf6DHDgqvhwFHNkGeXYxUAtGD7mHKu9Uq")
    with pytest.raises(ValueError):
        validate_withdrawal_address("DOGE", eth)


def test_password_policy():
    ok = RegisterRequest(email="a@b.co", password="Str0ng!pass")
    assert ok.password == "Str0ng!pass"
    cases = {
        "alllowercase1!": "uppercase",
        "ALLUPPERCASE1!": "lowercase",
        "NoDigitsHere!": "digit",
        "NoSpecials123": "special",
        "Sh0r!t": "at least 8",
    }
    for pwd, hint in cases.items():
        with pytest.raises(ValidationError):
            RegisterRequest(email="a@b.co", password=pwd)
    # blacklist matches through case and decoration
    with pytest.raises(ValidationError, match="too common"):
        RegisterRequest(email="a@b.co", password="Password123!")
    with pytest.raises(ValidationError, match="too common"):
        RegisterRequest(email="a@b.co", password="Qwerty123!")


@pytest.fixture
def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_duplicate_active_schedule_rejected(db, user, client):
    H = {"Authorization": f"Bearer {create_access_token(str(user.id)).access_token}"}
    async with client as c:
        r1 = await c.post("/api/purchases/", headers=H,
                          json={"crypto": "BTC", "amount_usdt": 50, "frequency": "weekly"})
        assert r1.status_code == 201, r1.text
        r2 = await c.post("/api/purchases/", headers=H,
                          json={"crypto": "BTC", "amount_usdt": 75, "frequency": "daily"})
        assert r2.status_code == 422
        assert "already exists" in r2.text
        # a different coin is fine
        r3 = await c.post("/api/purchases/", headers=H,
                          json={"crypto": "ETH", "amount_usdt": 50, "frequency": "weekly"})
        assert r3.status_code == 201, r3.text
        # invalid amount via API -> 422 from Pydantic
        r4 = await c.post("/api/purchases/", headers=H,
                          json={"crypto": "SOL", "amount_usdt": 5, "frequency": "weekly"})
        assert r4.status_code == 422
