"""Multi-exchange behavior of the bot core: exchange selection, per-venue
config validation, and the multi-venue selftest. No network — venue clients
are only constructed (their __init__ makes no HTTP calls) or stubbed.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autobuy
from autobuy import SubAccountWorker, load_config
from exchange_errors import ExchangeAPIError
from binance_client import BinanceClient
from bitget_client import BitgetClient
from bybit_client import BybitClient
from okx_client import OKXClient

BASE_CFG = {
    "name": "sub1",
    "api_key": "k",
    "api_secret": "s",
    "api_passphrase": "p",
    "buy_coin": "ETH",
    "min_usdt_to_trigger": 5,
}


class TestExchangeSelection(unittest.TestCase):
    def test_default_is_bitget(self):
        worker = SubAccountWorker(BASE_CFG)
        self.assertIsInstance(worker.client, BitgetClient)
        self.assertEqual(worker.exchange, "bitget")
        self.assertEqual(worker.symbol, "ETHUSDT")

    def test_bybit(self):
        worker = SubAccountWorker(dict(BASE_CFG, exchange="bybit"))
        self.assertIsInstance(worker.client, BybitClient)
        self.assertEqual(worker.symbol, "ETHUSDT")

    def test_okx_symbol_uses_hyphen(self):
        worker = SubAccountWorker(dict(BASE_CFG, exchange="okx"))
        self.assertIsInstance(worker.client, OKXClient)
        self.assertEqual(worker.symbol, "ETH-USDT")

    def test_binance(self):
        worker = SubAccountWorker(dict(BASE_CFG, exchange="binance"))
        self.assertIsInstance(worker.client, BinanceClient)
        self.assertEqual(worker.symbol, "ETHUSDT")

    def test_exchange_name_case_insensitive(self):
        worker = SubAccountWorker(dict(BASE_CFG, exchange="Bybit"))
        self.assertIsInstance(worker.client, BybitClient)


class TestConfigValidation(unittest.TestCase):
    def _write(self, cfg):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(cfg, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_legacy_config_without_exchange_defaults_to_bitget(self):
        cfg = load_config(self._write({"subaccounts": [BASE_CFG]}))
        self.assertEqual(cfg["subaccounts"][0]["exchange"], "bitget")

    def test_bybit_needs_no_passphrase(self):
        sub = {k: v for k, v in BASE_CFG.items() if k != "api_passphrase"}
        sub["exchange"] = "bybit"
        cfg = load_config(self._write({"subaccounts": [sub]}))
        self.assertEqual(cfg["subaccounts"][0]["exchange"], "bybit")

    def test_binance_needs_no_passphrase(self):
        sub = {k: v for k, v in BASE_CFG.items() if k != "api_passphrase"}
        sub["exchange"] = "binance"
        cfg = load_config(self._write({"subaccounts": [sub]}))
        self.assertEqual(cfg["subaccounts"][0]["exchange"], "binance")

    def test_bitget_still_requires_passphrase(self):
        sub = {k: v for k, v in BASE_CFG.items() if k != "api_passphrase"}
        with self.assertRaisesRegex(ValueError, "api_passphrase"):
            load_config(self._write({"subaccounts": [sub]}))

    def test_okx_requires_passphrase(self):
        sub = {k: v for k, v in BASE_CFG.items() if k != "api_passphrase"}
        sub["exchange"] = "okx"
        with self.assertRaisesRegex(ValueError, "api_passphrase"):
            load_config(self._write({"subaccounts": [sub]}))

    def test_unknown_exchange_entry_skipped(self):
        bad = dict(BASE_CFG, name="bad", exchange="kraken")
        path = self._write({"subaccounts": [BASE_CFG, bad]})
        cfg = load_config(path)
        self.assertEqual([s["name"] for s in cfg["subaccounts"]], ["sub1"])

    def test_unknown_exchange_alone_rejected(self):
        bad = dict(BASE_CFG, exchange="kraken")
        with self.assertRaisesRegex(ValueError, "kraken"):
            load_config(self._write({"subaccounts": [bad]}))

    def test_exchange_normalized_to_lowercase(self):
        cfg = load_config(self._write({"subaccounts": [dict(BASE_CFG, exchange="OKX")]}))
        self.assertEqual(cfg["subaccounts"][0]["exchange"], "okx")


class StubVenue:
    """Stands in for any exchange client during selftest tests."""

    def __init__(self, reachable=True, skew_ms=0):
        self.reachable = reachable
        self.skew_ms = skew_ms

    @staticmethod
    def spot_symbol(base, quote):
        return f"{base}{quote}"

    def get_server_time(self):
        if not self.reachable:
            raise ExchangeAPIError("NETWORK", "blocked")
        return int(time.time() * 1000) + self.skew_ms

    def get_symbol_info(self, symbol):
        return {"status": "online", "minTradeUSDT": "1", "quotePrecision": "4"}

    def get_ticker_price(self, symbol):
        return 50000.0


class TestMultiVenueSelftest(unittest.TestCase):
    def _patch(self, stubs):
        return mock.patch.object(
            autobuy, "make_client",
            lambda exchange, **kw: stubs[exchange])

    def test_all_venues_reachable_passes(self):
        stubs = {ex: StubVenue() for ex in autobuy.EXCHANGE_CLIENTS}
        with self._patch(stubs):
            self.assertEqual(autobuy.selftest(), 0)

    def test_one_unreachable_venue_fails(self):
        stubs = {ex: StubVenue() for ex in autobuy.EXCHANGE_CLIENTS}
        stubs["binance"] = StubVenue(reachable=False)
        with self._patch(stubs):
            self.assertEqual(autobuy.selftest(), 1)

    def test_clock_skew_fails(self):
        stubs = {ex: StubVenue(skew_ms=10_000) for ex in autobuy.EXCHANGE_CLIENTS}
        with self._patch(stubs):
            self.assertEqual(autobuy.selftest(), 1)


if __name__ == "__main__":
    unittest.main()
