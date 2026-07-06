"""Unit tests for the auto-buy bot. All Bitget HTTP calls are mocked — no
network or real credentials needed.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autobuy
from autobuy import SubAccountWorker, load_config
from bitget_client import BitgetAPIError, BitgetClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def ok(data):
    return FakeResponse({"code": "00000", "msg": "success", "data": data})


SUB_CFG = {
    "name": "sub1",
    "api_key": "k",
    "api_secret": "s",
    "api_passphrase": "p",
    "buy_coin": "BTC",
    "min_usdt_to_trigger": 5,
}

SYMBOL_DATA = [{
    "symbol": "BTCUSDT",
    "status": "online",
    "minTradeUSDT": "1",
    "quotePrecision": "4",
}]


class FakeBitgetHTTP:
    """Routes BitgetClient requests to canned responses and records orders."""

    def __init__(self, usdt_available="123.456789"):
        self.usdt_available = usdt_available
        self.orders = []

    # Patched over requests.Session.request; non-function objects skip the
    # descriptor protocol, so no `session` argument arrives here.
    def __call__(self, method, url, data=None, headers=None, timeout=None):
        if "/spot/account/assets" in url:
            return ok([{"coin": "USDT", "available": self.usdt_available}])
        if "/spot/public/symbols" in url:
            return ok(SYMBOL_DATA)
        if "/spot/trade/place-order" in url:
            body = json.loads(data)
            self.orders.append(body)
            return ok({"orderId": str(1000 + len(self.orders)), "clientOid": body.get("clientOid")})
        if "/spot/trade/orderInfo" in url:
            return ok([{"status": "filled", "baseVolume": "0.002", "quoteVolume": "123.4567"}])
        raise AssertionError(f"unexpected request: {method} {url}")


def patch_http(fake):
    return mock.patch("requests.Session.request", new=fake)


class TestSigning(unittest.TestCase):
    def test_signature_matches_reference(self):
        client = BitgetClient("key", "secret", "pass")
        ts = "1700000000000"
        path = "/api/v2/spot/trade/place-order"
        body = '{"a":"b"}'
        expected = base64.b64encode(
            hmac.new(b"secret", (ts + "POST" + path + body).encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(client._sign(ts, "POST", path, body), expected)

    def test_error_code_raises(self):
        client = BitgetClient("k", "s", "p")
        with mock.patch(
            "requests.Session.request",
            return_value=FakeResponse({"code": "40001", "msg": "invalid key"}, status_code=400),
        ):
            with self.assertRaises(BitgetAPIError) as ctx:
                client.get_spot_available("USDT")
            self.assertEqual(ctx.exception.code, "40001")


class TestConfig(unittest.TestCase):
    def _write(self, cfg):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(cfg, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_valid_config_loads(self):
        cfg = load_config(self._write({"subaccounts": [SUB_CFG]}))
        self.assertEqual(cfg["subaccounts"][0]["name"], "sub1")

    def test_missing_fields_rejected(self):
        path = self._write({"subaccounts": [{"name": "x", "api_key": "k", "buy_coin": "BTC"}]})
        with self.assertRaisesRegex(ValueError, "api_secret"):
            load_config(path)

    def test_empty_subaccounts_rejected(self):
        with self.assertRaisesRegex(ValueError, "subaccounts"):
            load_config(self._write({"subaccounts": []}))

    def test_env_var_expansion(self):
        sub = dict(SUB_CFG, api_key="${TEST_AUTOBUY_KEY}", api_secret="${TEST_AUTOBUY_SECRET}")
        path = self._write({"subaccounts": [sub]})
        with mock.patch.dict(os.environ, {"TEST_AUTOBUY_KEY": "realkey", "TEST_AUTOBUY_SECRET": "realsecret"}):
            cfg = load_config(path)
        self.assertEqual(cfg["subaccounts"][0]["api_key"], "realkey")
        self.assertEqual(cfg["subaccounts"][0]["api_secret"], "realsecret")

    def test_partial_config_keeps_valid_entries(self):
        """An entry awaiting API keys is skipped; the rest keep working."""
        incomplete = {"name": "new-client", "buy_coin": "ETH",
                      "api_key": "${DEFINITELY_UNSET_VAR_X}", "api_secret": "", "api_passphrase": ""}
        path = self._write({"subaccounts": [SUB_CFG, incomplete]})
        cfg = load_config(path)
        self.assertEqual([s["name"] for s in cfg["subaccounts"]], ["sub1"])

    def test_unset_env_var_rejected(self):
        sub = dict(SUB_CFG, api_key="${TEST_AUTOBUY_DEFINITELY_UNSET}")
        path = self._write({"subaccounts": [sub]})
        os.environ.pop("TEST_AUTOBUY_DEFINITELY_UNSET", None)
        with self.assertRaisesRegex(ValueError, "TEST_AUTOBUY_DEFINITELY_UNSET"):
            load_config(path)


class TestWorkerTick(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(autobuy.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_buys_full_spendable_floored_to_precision(self):
        fake = FakeBitgetHTTP(usdt_available="123.456789")
        with patch_http(fake):
            SubAccountWorker(SUB_CFG).tick()
        self.assertEqual(len(fake.orders), 1)
        order = fake.orders[0]
        self.assertEqual(order["symbol"], "BTCUSDT")
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["orderType"], "market")
        self.assertEqual(order["size"], "123.4567")

    def test_dry_run_places_no_order(self):
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            SubAccountWorker(SUB_CFG, dry_run=True).tick()
        self.assertEqual(fake.orders, [])

    def test_below_trigger_skips(self):
        fake = FakeBitgetHTTP(usdt_available="3.0")
        with patch_http(fake):
            SubAccountWorker(SUB_CFG).tick()
        self.assertEqual(fake.orders, [])

    def test_below_exchange_minimum_skips(self):
        # Above the user trigger (5) is irrelevant here: trigger 2, balance 3,
        # but exchange minTradeUSDT is 1 → order of 3 is fine; instead test
        # trigger 0.5 with balance 0.8 (below minTradeUSDT=1).
        cfg = dict(SUB_CFG, min_usdt_to_trigger=0.5)
        fake = FakeBitgetHTTP(usdt_available="0.8")
        with patch_http(fake):
            SubAccountWorker(cfg).tick()
        self.assertEqual(fake.orders, [])

    def test_reserve_and_cap_respected(self):
        cfg = dict(SUB_CFG, reserve_usdt=100, max_usdt_per_order=10)
        fake = FakeBitgetHTTP(usdt_available="123.456789")
        with patch_http(fake):
            SubAccountWorker(cfg).tick()
        self.assertEqual(fake.orders[0]["size"], "10.0000")

    def test_reserve_leaves_balance_untouched(self):
        cfg = dict(SUB_CFG, reserve_usdt=120)
        fake = FakeBitgetHTTP(usdt_available="123.0")  # spendable 3 < trigger 5
        with patch_http(fake):
            SubAccountWorker(cfg).tick()
        self.assertEqual(fake.orders, [])

    def test_disabled_subaccount_does_nothing(self):
        cfg = dict(SUB_CFG, enabled=False)
        fake = FakeBitgetHTTP()
        with patch_http(fake):
            SubAccountWorker(cfg).tick()
        self.assertEqual(fake.orders, [])

    def test_lowercase_coin_normalized(self):
        worker = SubAccountWorker(dict(SUB_CFG, buy_coin="btc"))
        self.assertEqual(worker.symbol, "BTCUSDT")

    def test_api_error_propagates_for_loop_to_catch(self):
        def failing(session_self, method, url, **kwargs):
            return FakeResponse({"code": "40009", "msg": "sign error"}, status_code=400)

        with mock.patch("requests.Session.request", new=failing):
            with self.assertRaises(BitgetAPIError):
                SubAccountWorker(SUB_CFG).tick()


class TestSelftest(unittest.TestCase):
    """--selftest needs no config or credentials; it must hit only public endpoints."""

    def _fake(self, server_time_offset_ms=0, reachable=True):
        # A plain function patched over Session.request gets bound, so the
        # session instance arrives as the first argument.
        def handler(session, method, url, data=None, headers=None, timeout=None):
            self.assertIsNone(headers.get("ACCESS-KEY"), "selftest must not send auth headers")
            if not reachable:
                raise requests.exceptions.ConnectionError("blocked")
            if "/public/time" in url:
                now_ms = int(autobuy.time.time() * 1000) + server_time_offset_ms
                return ok({"serverTime": str(now_ms)})
            if "/spot/public/symbols" in url:
                return ok(SYMBOL_DATA)
            if "/spot/market/tickers" in url:
                return ok([{"lastPr": "50000.0"}])
            raise AssertionError(f"selftest hit unexpected endpoint: {url}")
        return handler

    def test_passes_with_good_connectivity_and_clock(self):
        with mock.patch("requests.Session.request", new=self._fake()):
            self.assertEqual(autobuy.selftest(), 0)

    def test_fails_when_unreachable(self):
        with mock.patch("requests.Session.request", new=self._fake(reachable=False)):
            self.assertEqual(autobuy.selftest(), 1)

    def test_fails_on_large_clock_skew(self):
        with mock.patch("requests.Session.request", new=self._fake(server_time_offset_ms=10_000)):
            self.assertEqual(autobuy.selftest(), 1)


if __name__ == "__main__":
    unittest.main()
