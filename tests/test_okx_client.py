"""Unit tests for the OKX v5 spot client. All HTTP calls are mocked — no
network or real credentials needed.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock
from urllib.parse import urlsplit

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from okx_client import OKXAPIError, OKXClient

API_KEY = "test-api-key"
API_SECRET = "test-api-secret"
PASSPHRASE = "test-passphrase"
# Timestamp format from the official docs' authentication example.
FROZEN_TS = "2020-12-08T09:08:57.715Z"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def ok(data):
    return FakeResponse({"code": "0", "msg": "", "data": data})


def make_client():
    return OKXClient(api_key=API_KEY, api_secret=API_SECRET, passphrase=PASSPHRASE)


class TestSignature(unittest.TestCase):
    @mock.patch.object(OKXClient, "_timestamp", return_value=FROZEN_TS)
    @mock.patch("requests.Session.request")
    def test_signed_get_headers_match_reference(self, req, _ts):
        """Expected signature built independently of the client's signer:
        base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body))
        with requestPath including the ?query string (per the OKX docs)."""
        req.return_value = ok([{"details": [{"ccy": "USDT", "availBal": "0"}]}])
        make_client().get_spot_available("USDT")

        method, url = req.call_args.args[0], req.call_args.args[1]
        headers = req.call_args.kwargs["headers"]
        self.assertEqual(method, "GET")
        split = urlsplit(url)
        self.assertEqual(split.path, "/api/v5/account/balance")
        self.assertEqual(split.query, "ccy=USDT")

        prehash = FROZEN_TS + "GET" + "/api/v5/account/balance?ccy=USDT" + ""
        expected_sign = base64.b64encode(
            hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(headers["OK-ACCESS-SIGN"], expected_sign)
        self.assertEqual(headers["OK-ACCESS-KEY"], API_KEY)
        self.assertEqual(headers["OK-ACCESS-PASSPHRASE"], PASSPHRASE)
        self.assertEqual(headers["OK-ACCESS-TIMESTAMP"], FROZEN_TS)

    def test_timestamp_is_iso8601_utc_with_milliseconds(self):
        ts = OKXClient._timestamp()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    @mock.patch.object(OKXClient, "_timestamp", return_value=FROZEN_TS)
    @mock.patch("requests.Session.request")
    def test_post_signature_covers_json_body(self, req, _ts):
        req.return_value = ok([{"ordId": "1", "clOrdId": "", "sCode": "0", "sMsg": ""}])
        make_client().place_market_buy("XLM-USDT", 15)

        body = req.call_args.kwargs["data"]
        prehash = FROZEN_TS + "POST" + "/api/v5/trade/order" + body
        expected_sign = base64.b64encode(
            hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(req.call_args.kwargs["headers"]["OK-ACCESS-SIGN"], expected_sign)


class TestOrders(unittest.TestCase):
    @mock.patch("requests.Session.request")
    def test_place_market_buy_is_quote_denominated(self, req):
        req.return_value = ok([{"ordId": "312269865356374016",
                                "clOrdId": "autobuy0123456789abcdef0123",
                                "sCode": "0", "sMsg": ""}])
        result = make_client().place_market_buy(
            "XLM-USDT", 15, client_oid="autobuy-0123456789abcdef0123")
        self.assertEqual(result["orderId"], "312269865356374016")

        method, url = req.call_args.args[0], req.call_args.args[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://www.okx.com/api/v5/trade/order")
        body = json.loads(req.call_args.kwargs["data"])
        self.assertEqual(body["instId"], "XLM-USDT")
        self.assertEqual(body["tdMode"], "cash")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["ordType"], "market")
        self.assertEqual(body["sz"], "15")
        # sz must be the QUOTE amount (spend 15 USDT), never a base quantity.
        self.assertEqual(body["tgtCcy"], "quote_ccy")
        self.assertEqual(body["clOrdId"], "autobuy0123456789abcdef0123")

    @mock.patch("requests.Session.request")
    def test_client_oid_sanitization(self, req):
        req.return_value = ok([{"ordId": "1", "clOrdId": "x", "sCode": "0", "sMsg": ""}])
        # Illegal chars stripped, then truncated to 32 alphanumerics.
        make_client().place_market_buy("XLM-USDT", 15,
                                       client_oid="autobuy #1!@-" + "x" * 40)
        oid = json.loads(req.call_args.kwargs["data"])["clOrdId"]
        self.assertEqual(oid, ("autobuy1" + "x" * 40)[:32])
        self.assertLessEqual(len(oid), 32)
        self.assertRegex(oid, r"^[A-Za-z0-9]{1,32}$")

    @mock.patch("requests.Session.request")
    def test_order_scode_error_raises_with_venue_code(self, req):
        # Failed orders come back HTTP 200 with a generic top-level code "1"
        # and the real reason in data[0].sCode / sMsg.
        req.return_value = FakeResponse({
            "code": "1", "msg": "Operation failed.",
            "data": [{"ordId": "", "clOrdId": "", "sCode": "51008",
                      "sMsg": "Order failed. Insufficient USDT balance in account."}]})
        with self.assertRaises(OKXAPIError) as ctx:
            make_client().place_market_buy("XLM-USDT", 15)
        self.assertEqual(ctx.exception.code, "51008")
        self.assertEqual(OKXAPIError.exchange, "OKX")

    @mock.patch("requests.Session.request")
    def test_get_order_info_normalization(self, req):
        req.return_value = ok([{
            "instId": "XLM-USDT", "ordId": "312269865356374016",
            "state": "filled", "accFillSz": "34.6", "avgPx": "0.4335",
            "sz": "15", "tgtCcy": "quote_ccy", "ordType": "market", "side": "buy"}])
        info = make_client().get_order_info("XLM-USDT", "312269865356374016")
        self.assertEqual(info["status"], "filled")
        self.assertAlmostEqual(info["baseVolume"], 34.6)
        # No direct fill-notional field on OKX: quoteVolume = accFillSz * avgPx.
        self.assertAlmostEqual(info["quoteVolume"], 34.6 * 0.4335)

        url = req.call_args.args[1]
        self.assertIn("instId=XLM-USDT", url)
        self.assertIn("ordId=312269865356374016", url)

    @mock.patch("requests.Session.request")
    def test_get_order_info_maps_canceled_to_cancelled(self, req):
        req.return_value = ok([{"state": "canceled", "accFillSz": "0", "avgPx": ""}])
        info = make_client().get_order_info("XLM-USDT", "1")
        self.assertEqual(info["status"], "cancelled")
        self.assertEqual(info["quoteVolume"], 0.0)

    @mock.patch("requests.Session.request")
    def test_get_order_info_unknown_state_passes_through(self, req):
        req.return_value = ok([{"state": "weird_state", "accFillSz": "0", "avgPx": "0"}])
        self.assertEqual(make_client().get_order_info("XLM-USDT", "1")["status"],
                         "weird_state")

    @mock.patch("requests.Session.request")
    def test_get_order_info_not_found_returns_none(self, req):
        req.return_value = FakeResponse(
            {"code": "51603", "msg": "Order does not exist", "data": []})
        self.assertIsNone(make_client().get_order_info("XLM-USDT", "999"))


class TestPublicAndAccount(unittest.TestCase):
    @mock.patch("requests.Session.request")
    def test_get_spot_available_parses_balance_payload(self, req):
        # Realistic /api/v5/account/balance shape (Trading account) from docs.
        req.return_value = ok([{
            "adjEq": "", "totalEq": "91.88", "uTime": "1597026383085",
            "details": [{"ccy": "USDT", "eq": "91.88", "cashBal": "91.88",
                         "availBal": "91.87903", "frozenBal": "0",
                         "ordFrozen": "0"}]}])
        self.assertAlmostEqual(make_client().get_spot_available("usdt"), 91.87903)

    @mock.patch("requests.Session.request")
    def test_get_spot_available_missing_ccy_is_zero(self, req):
        req.return_value = ok([{"details": []}])
        self.assertEqual(make_client().get_spot_available("USDT"), 0.0)

    @mock.patch("requests.Session.request")
    def test_get_symbol_info_normalization(self, req):
        instruments = ok([{
            "instType": "SPOT", "instId": "XLM-USDT", "baseCcy": "XLM",
            "quoteCcy": "USDT", "state": "live", "minSz": "10",
            "lotSz": "0.000001", "tickSz": "0.0001"}])
        ticker = ok([{"instId": "XLM-USDT", "last": "0.5"}])
        req.side_effect = [instruments, ticker]
        info = make_client().get_symbol_info("XLM-USDT")
        self.assertEqual(info["status"], "online")
        # minSz is BASE-denominated: 10 XLM * 0.5 USDT = 5 USDT minimum.
        self.assertAlmostEqual(float(info["minTradeUSDT"]), 5.0)
        self.assertEqual(info["quotePrecision"], 4)

    @mock.patch("requests.Session.request")
    def test_get_symbol_info_unknown_symbol(self, req):
        req.return_value = ok([])
        with self.assertRaises(OKXAPIError) as ctx:
            make_client().get_symbol_info("NOPE-USDT")
        self.assertEqual(ctx.exception.code, "SYMBOL")

    @mock.patch("requests.Session.request")
    def test_business_error_raises_with_venue_code(self, req):
        req.return_value = FakeResponse(
            {"code": "51001", "msg": "Instrument ID doesn't exist", "data": []})
        with self.assertRaises(OKXAPIError) as ctx:
            make_client().get_ticker_price("NOPE-USDT")
        self.assertEqual(ctx.exception.code, "51001")

    @mock.patch("requests.Session.request")
    def test_get_server_time_and_ticker(self, req):
        req.side_effect = [
            ok([{"ts": "1597026383085"}]),
            ok([{"instId": "XLM-USDT", "last": "0.4321"}]),
        ]
        client = make_client()
        self.assertEqual(client.get_server_time(), 1597026383085)
        self.assertEqual(client.get_ticker_price("XLM-USDT"), 0.4321)
        self.assertEqual(OKXClient.spot_symbol("xlm", "usdt"), "XLM-USDT")

    @mock.patch("requests.Session.request",
                side_effect=requests.exceptions.ConnectionError("boom"))
    def test_network_error_raises(self, _req):
        with self.assertRaises(OKXAPIError) as ctx:
            make_client().get_server_time()
        self.assertEqual(ctx.exception.code, "NETWORK")

    @mock.patch("requests.Session.request")
    def test_non_json_response_raises(self, req):
        bad = mock.Mock()
        bad.status_code = 502
        bad.text = "<html>Bad Gateway</html>"
        bad.json.side_effect = ValueError("no json")
        req.return_value = bad
        with self.assertRaises(OKXAPIError) as ctx:
            make_client().get_server_time()
        self.assertEqual(ctx.exception.code, "HTTP")
        self.assertEqual(ctx.exception.http_status, 502)


if __name__ == "__main__":
    unittest.main()
