import json

import httpx
import pytest

import api.services.sumsub_client as sc_mod
from api.services.sumsub_client import SumsubClient, SumsubError


@pytest.fixture
def transport_calls(monkeypatch):
    """Route SumsubClient's HTTP through a scripted MockTransport."""
    calls = []
    state = {"status_code": 200, "payload": {}}

    def handler(request):
        calls.append(request)
        return httpx.Response(state["status_code"], json=state["payload"])

    real = httpx.AsyncClient

    def factory(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(**kw)

    monkeypatch.setattr(sc_mod.httpx, "AsyncClient", factory)
    return calls, state


def make():
    return SumsubClient(app_token="tok", secret_key="sec", base_url="https://api.sumsub.test")


def test_configured_flag():
    assert make().configured
    assert not SumsubClient(app_token="", secret_key="", base_url="x").configured


def test_request_signing_headers():
    headers = make()._headers("POST", "/resources/applicants", '{"a":1}')
    assert headers["X-App-Token"] == "tok"
    assert len(headers["X-App-Access-Sig"]) == 64      # hex sha256
    assert headers["X-App-Access-Ts"].isdigit()


async def test_create_applicant(transport_calls):
    calls, state = transport_calls
    state["payload"] = {"id": "applicant-123"}
    applicant = await make().create_applicant("user-1", "a@b.co")
    assert applicant == "applicant-123"
    req = calls[0]
    assert req.method == "POST"
    assert "levelName=" in str(req.url)
    assert json.loads(req.content)["externalUserId"] == "user-1"


async def test_status_mapping(transport_calls):
    calls, state = transport_calls
    client = make()
    state["payload"] = {"reviewStatus": "completed", "reviewResult": {"reviewAnswer": "GREEN"}}
    assert await client.get_applicant_status("a1") == "verified"
    state["payload"] = {"reviewStatus": "completed", "reviewResult": {"reviewAnswer": "RED"}}
    assert await client.get_applicant_status("a1") == "rejected"
    state["payload"] = {"reviewStatus": "pending"}
    assert await client.get_applicant_status("a1") == "pending"


async def test_access_token(transport_calls):
    calls, state = transport_calls
    state["payload"] = {"token": "sdk-token"}
    assert await make().get_access_token("user-1") == "sdk-token"
    assert "accessTokens" in str(calls[0].url)


async def test_api_error_raises(transport_calls):
    calls, state = transport_calls
    state["status_code"] = 401
    state["payload"] = {"description": "bad signature"}
    with pytest.raises(SumsubError, match="401"):
        await make().create_applicant("u", "a@b.co")
