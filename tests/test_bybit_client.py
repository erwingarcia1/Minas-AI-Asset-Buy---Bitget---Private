"""Unit tests for bybit_client.py — no real network, everything mocked."""

import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bybit_client
from bybit_client import BybitAPIError, BybitClient


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def ok(result):
    return FakeResponse({"retCode": 0, "retMsg": "OK", "result": result})


API_KEY = "test-key"
API_SECRET = "test-secret"
FIXED_TIME = 1700000000.123  # -> X-BAPI-TIMESTAMP "1700000000123"


def make_client():
    return BybitClient(api_key=API_KEY, api_secret=API_SECRET, passphrase="ignored")


class TestBybitSigning(unittest.TestCase):
    @mock.patch("requests.Session.request")
    @mock.patch.object(bybit_client.time, "time", return_value=FIXED_TIME)
    def test_get_signature_reference(self, _time, req):
        """Expected signature computed independently with hmac/hashlib."""
        req.return_value = ok({"list": [{"coin": [
            {"coin": "USDT", "walletBalance": "10", "locked": "0"}]}]})
        make_client().get_spot_available("USDT")

        method, url = req.call_args[0][0], req.call_args[0][1]
        headers = req.call_args[1]["headers"]
        self.assertEqual(method, "GET")
        query = "accountType=UNIFIED&coin=USDT"
        self.assertTrue(url.endswith("/v5/account/wallet-balance?" + query))

        ts = "1700000000123"  # epoch ms, integer string
        expected = hmac.new(API_SECRET.encode(),
                            (ts + API_KEY + "5000" + query).encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-BAPI-TIMESTAMP"], ts)
        self.assertEqual(headers["X-BAPI-API-KEY"], API_KEY)
        self.assertEqual(headers["X-BAPI-RECV-WINDOW"], "5000")
        self.assertEqual(headers["X-BAPI-SIGN"], expected)

    @mock.patch("requests.Session.request")
    @mock.patch.object(bybit_client.time, "time", return_value=FIXED_TIME)
    def test_post_signature_reference(self, _time, req):
        """POST signs timestamp + key + recv_window + raw JSON body."""
        req.return_value = ok({"orderId": "1", "orderLinkId": "autobuy-1"})
        make_client().place_market_buy("XLMUSDT", 15, client_oid="autobuy-1")

        body_sent = req.call_args[1]["data"]
        expected_body = ('{"category":"spot","symbol":"XLMUSDT","side":"Buy",'
                         '"orderType":"Market","qty":"15","marketUnit":"quoteCoin",'
                         '"orderLinkId":"autobuy-1"}')
        self.assertEqual(body_sent, expected_body)

        ts = "1700000000123"
        expected = hmac.new(API_SECRET.encode(),
                            (ts + API_KEY + "5000" + expected_body).encode(),
                            hashlib.sha256).hexdigest()
        headers = req.call_args[1]["headers"]
        self.assertEqual(headers["X-BAPI-SIGN"], expected)
        self.assertEqual(headers["X-BAPI-TIMESTAMP"], ts)


class TestBybitOrders(unittest.TestCase):
    @mock.patch("requests.Session.request")
    def test_place_market_buy_is_quote_denominated(self, req):
        req.return_value = ok({"orderId": "167123", "orderLinkId": ""})
        result = make_client().place_market_buy("XLMUSDT", 15.5)

        self.assertEqual(req.call_args[0][0], "POST")
        self.assertTrue(req.call_args[0][1].endswith("/v5/order/create"))
        body = json.loads(req.call_args[1]["data"])
        self.assertEqual(body["category"], "spot")
        self.assertEqual(body["symbol"], "XLMUSDT")
        self.assertEqual(body["side"], "Buy")
        self.assertEqual(body["orderType"], "Market")
        self.assertEqual(body["qty"], "15.5")           # quote amount, as string
        self.assertEqual(body["marketUnit"], "quoteCoin")  # explicit quote unit
        self.assertNotIn("orderLinkId", body)
        self.assertEqual(result["orderId"], "167123")

    @mock.patch("requests.Session.request")
    def test_client_oid_sanitized(self, req):
        req.return_value = ok({"orderId": "1"})
        oid = "autobuy.0123456789:abcdef0123456789abcd!" + "x" * 30
        make_client().place_market_buy("XLMUSDT", 10, client_oid=oid)
        sent = json.loads(req.call_args[1]["data"])["orderLinkId"]
        self.assertLessEqual(len(sent), 36)              # Bybit max 36 chars
        self.assertRegex(sent, r"^[A-Za-z0-9_-]+$")      # allowed charset
        self.assertTrue(sent.startswith("autobuy0123456789abcdef"))

    @mock.patch("requests.Session.request")
    def test_get_order_info_normalized(self, req):
        req.return_value = ok({"list": [{
            "orderId": "167123", "orderLinkId": "autobuy-1",
            "orderStatus": "Filled", "cumExecQty": "120.5",
            "cumExecValue": "14.99", "avgPrice": "0.1244"}]})
        info = make_client().get_order_info("XLMUSDT", "167123")
        self.assertEqual(info, {"status": "filled",
                                "baseVolume": 120.5, "quoteVolume": 14.99})

    @mock.patch("requests.Session.request")
    def test_get_order_info_history_fallback_and_none(self, req):
        filled = {"orderStatus": "PartiallyFilledCanceled",
                  "cumExecQty": "5", "cumExecValue": "0.6"}
        req.side_effect = [ok({"list": []}), ok({"list": [filled]})]
        info = make_client().get_order_info("XLMUSDT", "42")
        self.assertEqual(info["status"], "cancelled")
        self.assertTrue(req.call_args_list[0][0][1]
                        .startswith(make_client().base_url + "/v5/order/realtime?"))
        self.assertTrue(req.call_args_list[1][0][1]
                        .startswith(make_client().base_url + "/v5/order/history?"))

        req.side_effect = [ok({"list": []}), ok({"list": []})]
        self.assertIsNone(make_client().get_order_info("XLMUSDT", "42"))


class TestBybitAccountAndMarket(unittest.TestCase):
    @mock.patch("requests.Session.request")
    def test_get_spot_available_unified(self, req):
        # Realistic UTA wallet-balance payload (availableToWithdraw is
        # deprecated/empty for UNIFIED since 2025-01-09).
        req.return_value = ok({"list": [{
            "accountType": "UNIFIED",
            "totalEquity": "3.31216591",
            "coin": [{"coin": "USDT", "equity": "125.5",
                      "walletBalance": "125.5", "locked": "25.5",
                      "availableToWithdraw": "", "free": "",
                      "borrowAmount": "0", "accruedInterest": "0"}],
        }]})
        self.assertEqual(make_client().get_spot_available("USDT"), 100.0)
        self.assertEqual(make_client().get_spot_available("XLM"), 0.0)

    @mock.patch("requests.Session.request")
    def test_get_spot_available_classic_fallback(self, req):
        req.side_effect = [
            FakeResponse({"retCode": 10001, "retMsg": "accountType only support UNIFIED.",
                          "result": {}}),
            ok({"list": [{"accountType": "SPOT",
                          "coin": [{"coin": "USDT", "walletBalance": "50",
                                    "free": "48.75", "locked": "1.25"}]}]}),
        ]
        self.assertEqual(make_client().get_spot_available("USDT"), 48.75)
        self.assertIn("accountType=SPOT", req.call_args_list[1][0][1])

    @mock.patch("requests.Session.request")
    def test_get_symbol_info_normalized(self, req):
        # Realistic instruments-info payload from the v5 docs.
        req.return_value = ok({"category": "spot", "list": [{
            "symbol": "XLMUSDT", "baseCoin": "XLM", "quoteCoin": "USDT",
            "status": "Trading",
            "lotSizeFilter": {"basePrecision": "0.1", "quotePrecision": "0.0001",
                              "minOrderQty": "10", "maxOrderQty": "1480676.8",
                              "minOrderAmt": "1", "maxOrderAmt": "2000000"},
            "priceFilter": {"tickSize": "0.00001"},
        }]})
        info = make_client().get_symbol_info("XLMUSDT")
        self.assertEqual(info["status"], "online")
        self.assertEqual(info["minTradeUSDT"], "1")
        self.assertEqual(info["quotePrecision"], 4)

    @mock.patch("requests.Session.request")
    def test_get_symbol_info_unknown_symbol(self, req):
        req.return_value = ok({"category": "spot", "list": []})
        with self.assertRaises(BybitAPIError) as ctx:
            make_client().get_symbol_info("NOPEUSDT")
        self.assertEqual(ctx.exception.code, "SYMBOL")

    @mock.patch("requests.Session.request")
    def test_public_endpoints(self, req):
        req.return_value = ok({"timeSecond": "1688639403",
                               "timeNano": "1688639403423213947"})
        self.assertEqual(make_client().get_server_time(), 1688639403000)

        req.return_value = ok({"list": [{"symbol": "XLMUSDT",
                                         "lastPrice": "0.1244"}]})
        self.assertEqual(make_client().get_ticker_price("XLMUSDT"), 0.1244)
        self.assertEqual(BybitClient.spot_symbol("XLM", "USDT"), "XLMUSDT")

    @mock.patch("requests.Session.request")
    def test_business_error_raises(self, req):
        req.return_value = FakeResponse(
            {"retCode": 10004, "retMsg": "error sign! origin_string[...]",
             "result": {}}, status_code=200)
        with self.assertRaises(BybitAPIError) as ctx:
            make_client().get_ticker_price("XLMUSDT")
        self.assertEqual(ctx.exception.code, "10004")
        self.assertEqual(ctx.exception.exchange, "Bybit")

    @mock.patch("requests.Session.request")
    def test_non_json_raises_http(self, req):
        req.return_value = FakeResponse(ValueError("no json"),
                                        status_code=502, text="Bad Gateway")
        with self.assertRaises(BybitAPIError) as ctx:
            make_client().get_server_time()
        self.assertEqual(ctx.exception.code, "HTTP")
        self.assertEqual(ctx.exception.http_status, 502)


if __name__ == "__main__":
    unittest.main()
