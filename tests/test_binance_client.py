"""Unit tests for the Binance spot client. All HTTP calls are mocked — no
network or real credentials needed.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from binance_client import BinanceAPIError, BinanceClient

API_KEY = "test-api-key"
# Secret from the official Binance signed-endpoint example.
API_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
FROZEN_TIME = 1700000000.0  # -> timestamp=1700000000000


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def make_client():
    return BinanceClient(api_key=API_KEY, api_secret=API_SECRET)


class TestSignature(unittest.TestCase):
    def test_doc_reference_vector(self):
        # Exact example from the official REST API docs: HMAC SHA256 hex over
        # the urlencoded totalParams string.
        payload = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC"
                   "&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559")
        expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
        self.assertEqual(make_client()._sign(payload), expected)
        # Independently recompute the doc value with hmac/hashlib.
        self.assertEqual(
            hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest(),
            expected)

    @mock.patch("binance_client.time.time", return_value=FROZEN_TIME)
    @mock.patch("requests.Session.request")
    def test_signed_get_query_and_headers(self, req, _time):
        req.return_value = FakeResponse({"balances": []})
        make_client().get_spot_available("USDT")

        method, url = req.call_args.args[0], req.call_args.args[1]
        headers = req.call_args.kwargs["headers"]
        self.assertEqual(method, "GET")
        split = urlsplit(url)
        self.assertEqual(split.path, "/api/v3/account")
        self.assertEqual(headers["X-MBX-APIKEY"], API_KEY)

        # Expected signature built independently of the client's signer.
        unsigned = "omitZeroBalances=true&recvWindow=5000&timestamp=1700000000000"
        expected_sig = hmac.new(API_SECRET.encode(), unsigned.encode(),
                                hashlib.sha256).hexdigest()
        self.assertEqual(split.query, unsigned + "&signature=" + expected_sig)


class TestOrders(unittest.TestCase):
    @mock.patch("binance_client.time.time", return_value=FROZEN_TIME)
    @mock.patch("requests.Session.request")
    def test_place_market_buy_is_quote_denominated(self, req, _time):
        req.return_value = FakeResponse(
            {"symbol": "XLMUSDT", "orderId": 28, "status": "FILLED",
             "executedQty": "32.00000000", "cummulativeQuoteQty": "15.00000000"})
        oid = "autobuy-0123456789abcdef0123"
        result = make_client().place_market_buy("XLMUSDT", 15, client_oid=oid)
        self.assertEqual(result["orderId"], 28)

        method, url = req.call_args.args[0], req.call_args.args[1]
        kwargs = req.call_args.kwargs
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.binance.com/api/v3/order")
        self.assertEqual(kwargs["headers"]["Content-Type"],
                         "application/x-www-form-urlencoded")
        body = parse_qs(kwargs["data"], keep_blank_values=True)
        self.assertEqual(body["symbol"], ["XLMUSDT"])
        self.assertEqual(body["side"], ["BUY"])
        self.assertEqual(body["type"], ["MARKET"])
        self.assertEqual(body["quoteOrderQty"], ["15"])
        self.assertEqual(body["newClientOrderId"], [oid])
        self.assertEqual(body["recvWindow"], ["5000"])
        self.assertEqual(body["timestamp"], ["1700000000000"])
        self.assertNotIn("quantity", body)  # must never be base-denominated
        # Signature is computed over the body minus the trailing signature pair.
        unsigned, _, sig = kwargs["data"].rpartition("&signature=")
        self.assertEqual(sig, hmac.new(API_SECRET.encode(), unsigned.encode(),
                                       hashlib.sha256).hexdigest())

    @mock.patch("binance_client.time.time", return_value=FROZEN_TIME)
    @mock.patch("requests.Session.request")
    def test_client_oid_sanitization(self, req, _time):
        req.return_value = FakeResponse({"orderId": 1})
        # Illegal chars stripped, then truncated to 36 chars.
        make_client().place_market_buy("XLMUSDT", 15,
                                       client_oid="autobuy #1!@" + "x" * 40)
        body = parse_qs(req.call_args.kwargs["data"])
        oid = body["newClientOrderId"][0]
        self.assertEqual(oid, ("autobuy1" + "x" * 40)[:36])
        self.assertLessEqual(len(oid), 36)
        self.assertRegex(oid, r"^[\.A-Z\:/a-z0-9_-]{1,36}$")

    @mock.patch("requests.Session.request")
    def test_get_order_info_normalization(self, req):
        req.return_value = FakeResponse(
            {"symbol": "XLMUSDT", "orderId": 28, "status": "FILLED",
             "executedQty": "32.00000000",
             "cummulativeQuoteQty": "15.00000000", "type": "MARKET", "side": "BUY"})
        info = make_client().get_order_info("XLMUSDT", 28)
        self.assertEqual(info, {"status": "filled",
                                "baseVolume": "32.00000000",
                                "quoteVolume": "15.00000000"})

    @mock.patch("requests.Session.request")
    def test_get_order_info_not_found_returns_none(self, req):
        req.return_value = FakeResponse(
            {"code": -2013, "msg": "Order does not exist."}, status_code=400)
        self.assertIsNone(make_client().get_order_info("XLMUSDT", 999))


class TestPublicAndAccount(unittest.TestCase):
    @mock.patch("requests.Session.request")
    def test_get_spot_available_parses_account_payload(self, req):
        req.return_value = FakeResponse(
            {"makerCommission": 15, "takerCommission": 15, "canTrade": True,
             "accountType": "SPOT",
             "balances": [
                 {"asset": "BTC", "free": "4723846.89208129", "locked": "0.00000000"},
                 {"asset": "USDT", "free": "123.45000000", "locked": "1.00000000"}]})
        self.assertEqual(make_client().get_spot_available("usdt"), 123.45)
        self.assertEqual(make_client().get_spot_available("XLM"), 0.0)

    @mock.patch("requests.Session.request")
    def test_get_symbol_info_normalization(self, req):
        req.return_value = FakeResponse(
            {"timezone": "UTC", "serverTime": 1700000000000,
             "symbols": [{
                 "symbol": "XLMUSDT", "status": "TRADING",
                 "baseAsset": "XLM", "quoteAsset": "USDT",
                 "quotePrecision": 8, "quoteAssetPrecision": 8,
                 "filters": [
                     {"filterType": "PRICE_FILTER", "minPrice": "0.00010000"},
                     {"filterType": "NOTIONAL", "minNotional": "5.00000000",
                      "applyMinToMarket": True, "maxNotional": "9000000.00",
                      "applyMaxToMarket": False, "avgPriceMins": 5}]}]})
        info = make_client().get_symbol_info("XLMUSDT")
        self.assertEqual(info["status"], "online")
        self.assertEqual(info["minTradeUSDT"], "5.00000000")
        self.assertEqual(info["quotePrecision"], 8)

    @mock.patch("requests.Session.request")
    def test_get_symbol_info_invalid_symbol(self, req):
        req.return_value = FakeResponse(
            {"code": -1121, "msg": "Invalid symbol."}, status_code=400)
        with self.assertRaises(BinanceAPIError) as ctx:
            make_client().get_symbol_info("NOPEUSDT")
        self.assertEqual(ctx.exception.code, "SYMBOL")

    @mock.patch("requests.Session.request")
    def test_business_error_raises_with_venue_code(self, req):
        req.return_value = FakeResponse(
            {"code": -1121, "msg": "Invalid symbol."}, status_code=400)
        with self.assertRaises(BinanceAPIError) as ctx:
            make_client().get_ticker_price("NOPEUSDT")
        self.assertEqual(ctx.exception.code, "-1121")
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertEqual(BinanceAPIError.exchange, "Binance")

    @mock.patch("requests.Session.request")
    def test_get_server_time_and_ticker(self, req):
        req.side_effect = [
            FakeResponse({"serverTime": 1499827319559}),
            FakeResponse({"symbol": "XLMUSDT", "price": "0.09150000"}),
        ]
        client = make_client()
        self.assertEqual(client.get_server_time(), 1499827319559)
        self.assertEqual(client.get_ticker_price("XLMUSDT"), 0.0915)
        self.assertEqual(BinanceClient.spot_symbol("xlm", "usdt"), "XLMUSDT")


if __name__ == "__main__":
    unittest.main()
