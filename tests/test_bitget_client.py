"""bitget_client.py is the signed transport every live Bitget order and
withdrawal goes through. These two checks came from tests/test_autobuy.py
(the legacy bot suite, removed 2026-09-22); the module stayed, so its tests
stay with it: the request signature must match Bitget's reference recipe,
and an error envelope must surface as BitgetAPIError with Bitget's code."""
import base64
import hashlib
import hmac
import json
from unittest import mock

import pytest

from bitget_client import BitgetAPIError, BitgetClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_signature_matches_bitgets_reference_recipe():
    client = BitgetClient("key", "secret", "pass")
    ts = "1700000000000"
    path = "/api/v2/spot/trade/place-order"
    body = '{"a":"b"}'
    expected = base64.b64encode(
        hmac.new(b"secret", (ts + "POST" + path + body).encode(), hashlib.sha256).digest()
    ).decode()
    assert client._sign(ts, "POST", path, body) == expected


def test_error_envelope_raises_with_bitgets_code():
    client = BitgetClient("k", "s", "p")
    with mock.patch("requests.Session.request",
                    return_value=FakeResponse({"code": "40001", "msg": "invalid key"}, status_code=400)):
        with pytest.raises(BitgetAPIError) as ctx:
            client.get_spot_available("USDT")
    assert ctx.value.code == "40001"
