"""Tests for the admin web UI. Uses Flask's test client — no server, no network."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.app import (WEIGHT_MAX, WEIGHT_MIN, create_app, env_prefix, key_vars,
                        parse_ticker_field)

CLIENT = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "license_number": "LIC-123",
    "exchange": "Bitget",
    "subaccount": "sub1",
    "ticker": "btc",
    "min_usdt": "5",
    "enabled": "on",
}


class WebAppBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop("WEB_PASSWORD", None)
        os.environ.pop("AUTOBUY_CONFIG", None)
        self.app = create_app(data_dir=self.tmp.name)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def add_client(self, **overrides):
        data = dict(CLIENT, **overrides)
        return self.client.post("/add", data=data, follow_redirects=True)


class TestHelpers(unittest.TestCase):
    def test_env_prefix(self):
        self.assertEqual(env_prefix("sub1"), "SUB1")
        self.assertEqual(env_prefix("my-sub 2"), "MY_SUB_2")
        self.assertEqual(key_vars("sub1"),
                         ["SUB1_API_KEY", "SUB1_API_SECRET", "SUB1_API_PASSPHRASE"])

    def test_key_vars_per_exchange(self):
        """Bybit and Binance keys have no passphrase; Bitget and OKX do."""
        self.assertEqual(key_vars("sub1", "Bybit"), ["SUB1_API_KEY", "SUB1_API_SECRET"])
        self.assertEqual(key_vars("sub1", "Binance"), ["SUB1_API_KEY", "SUB1_API_SECRET"])
        self.assertEqual(key_vars("sub1", "OKX"),
                         ["SUB1_API_KEY", "SUB1_API_SECRET", "SUB1_API_PASSPHRASE"])
        self.assertEqual(key_vars("sub1", "Bitget"),
                         ["SUB1_API_KEY", "SUB1_API_SECRET", "SUB1_API_PASSPHRASE"])


class TestCrud(WebAppBase):
    def test_add_and_list(self):
        resp = self.add_client()
        self.assertEqual(resp.status_code, 200)
        page = self.client.get("/").get_data(as_text=True)
        for value in ("Jane Doe", "jane@example.com", "LIC-123", "Bitget", "sub1", "BTC"):
            self.assertIn(value, page)
        self.assertIn("awaiting keys", page)  # no env vars set

    def test_keys_badge_when_env_present(self):
        self.add_client()
        env = {"SUB1_API_KEY": "k", "SUB1_API_SECRET": "s", "SUB1_API_PASSPHRASE": "p"}
        with mock.patch.dict(os.environ, env):
            page = self.client.get("/").get_data(as_text=True)
        self.assertIn("configured", page)
        self.assertNotIn("awaiting keys", page)

    def test_validation_rejects_bad_ticker_and_email(self):
        self.add_client(ticker="not a ticker!!")
        self.add_client(email="not-an-email")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No clients on the desk yet", page)

    def test_edit_and_delete(self):
        self.add_client()
        self.client.post("/edit/1", data=dict(CLIENT, name="Janet Doe", ticker="ETH"))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Janet Doe", page)
        self.assertIn("ETH", page)
        self.client.post("/delete/1")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No clients on the desk yet", page)

    def test_csv_export(self):
        self.add_client()
        resp = self.client.get("/export.csv")
        body = resp.get_data(as_text=True)
        self.assertIn("Jane Doe", body)
        self.assertIn("License #", body)


class TestApply(WebAppBase):
    def test_apply_writes_bot_config_with_env_refs(self):
        self.add_client()
        self.add_client(name="Bob", email="bob@example.com", subaccount="my-sub 2",
                        ticker="SOL", enabled="")  # disabled — must not be exported
        resp = self.client.post("/apply", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(len(cfg["subaccounts"]), 1)
        sub = cfg["subaccounts"][0]
        self.assertEqual(sub["name"], "sub1")
        self.assertEqual(sub["buy_coin"], "BTC")
        self.assertEqual(sub["api_key"], "${SUB1_API_KEY}")
        self.assertEqual(sub["api_secret"], "${SUB1_API_SECRET}")
        self.assertEqual(sub["api_passphrase"], "${SUB1_API_PASSPHRASE}")

        page = resp.get_data(as_text=True)
        self.assertIn("Applied 1 client", page)
        self.assertIn("SUB1_API_KEY", page)  # missing-keys warning

    def test_apply_preserves_poll_seconds(self):
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"poll_seconds": 12, "subaccounts": []}, fh)
        self.add_client()
        self.client.post("/apply")
        with open(cfg_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["poll_seconds"], 12)

    def test_applied_config_loads_in_bot_when_keys_present(self):
        """End-to-end: UI apply -> bot load_config accepts the file."""
        import autobuy

        self.add_client()
        self.client.post("/apply")
        cfg_path = os.path.join(self.tmp.name, "config.json")
        env = {"SUB1_API_KEY": "k", "SUB1_API_SECRET": "s", "SUB1_API_PASSPHRASE": "p"}
        with mock.patch.dict(os.environ, env):
            cfg = autobuy.load_config(cfg_path)
        self.assertEqual(cfg["subaccounts"][0]["api_key"], "k")
        self.assertEqual(cfg["subaccounts"][0]["buy_coin"], "BTC")


class TestMultiExchange(WebAppBase):
    def test_form_accepts_all_supported_exchanges(self):
        for i, exchange in enumerate(("Bitget", "Bybit", "OKX", "Binance"), start=1):
            self.add_client(exchange=exchange, subaccount=f"sub{i}",
                            email=f"c{i}@example.com")
        page = self.client.get("/").get_data(as_text=True)
        for exchange in ("Bitget", "Bybit", "OKX", "Binance"):
            self.assertIn(exchange, page)

    def test_form_rejects_unknown_exchange(self):
        self.add_client(exchange="Kraken")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No clients on the desk yet", page)

    def test_apply_writes_exchange_and_omits_passphrase_for_bybit(self):
        self.add_client(exchange="Bybit", ticker="SOL")
        self.client.post("/apply")
        with open(os.path.join(self.tmp.name, "config.json"), encoding="utf-8") as fh:
            sub = json.load(fh)["subaccounts"][0]
        self.assertEqual(sub["exchange"], "bybit")
        self.assertEqual(sub["api_key"], "${SUB1_API_KEY}")
        self.assertNotIn("api_passphrase", sub)

    def test_bybit_keys_badge_needs_no_passphrase(self):
        self.add_client(exchange="Bybit")
        with mock.patch.dict(os.environ, {"SUB1_API_KEY": "k", "SUB1_API_SECRET": "s"}):
            page = self.client.get("/").get_data(as_text=True)
        self.assertIn("configured", page)

    def test_applied_bybit_config_loads_in_bot(self):
        """End-to-end: a Bybit client applied by the UI loads without a passphrase var."""
        import autobuy

        self.add_client(exchange="Bybit", ticker="SOL")
        self.client.post("/apply")
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with mock.patch.dict(os.environ, {"SUB1_API_KEY": "k", "SUB1_API_SECRET": "s"}):
            cfg = autobuy.load_config(cfg_path)
        self.assertEqual(cfg["subaccounts"][0]["exchange"], "bybit")
        self.assertEqual(cfg["subaccounts"][0]["buy_coin"], "SOL")


class TestBasketTicker(WebAppBase):
    def test_single_ticker_still_writes_buy_coin(self):
        self.add_client(ticker="xlm")
        self.client.post("/apply")
        with open(os.path.join(self.tmp.name, "config.json"), encoding="utf-8") as fh:
            sub = json.load(fh)["subaccounts"][0]
        self.assertEqual(sub["buy_coin"], "XLM")
        self.assertNotIn("basket", sub)

    def test_comma_separated_tickers_write_a_basket(self):
        self.add_client(ticker="xrp, xlm ,ada")
        self.client.post("/apply")
        with open(os.path.join(self.tmp.name, "config.json"), encoding="utf-8") as fh:
            sub = json.load(fh)["subaccounts"][0]
        self.assertNotIn("buy_coin", sub)
        self.assertEqual(sub["basket"],
                         [{"coin": "XRP", "weight": 1.0},
                          {"coin": "XLM", "weight": 1.0},
                          {"coin": "ADA", "weight": 1.0}])

    def test_weights_are_preserved(self):
        self.add_client(ticker="XRP:3,XLM:2")
        self.client.post("/apply")
        with open(os.path.join(self.tmp.name, "config.json"), encoding="utf-8") as fh:
            sub = json.load(fh)["subaccounts"][0]
        self.assertEqual(sub["basket"],
                         [{"coin": "XRP", "weight": 3.0}, {"coin": "XLM", "weight": 2.0}])

    def test_applied_basket_loads_in_the_bot(self):
        import autobuy

        self.add_client(ticker="XRP,XLM")
        self.client.post("/apply")
        cfg_path = os.path.join(self.tmp.name, "config.json")
        env = {"SUB1_API_KEY": "k", "SUB1_API_SECRET": "s", "SUB1_API_PASSPHRASE": "p"}
        with mock.patch.dict(os.environ, env):
            cfg = autobuy.load_config(cfg_path)
        self.assertEqual(autobuy.parse_basket(cfg["subaccounts"][0]),
                         [("XRP", 1.0), ("XLM", 1.0)])

    def test_bad_ticker_lists_are_rejected(self):
        for bad in ("XRP,,XRP", "XRP:0", "XRP:abc", "not a ticker!!", ","):
            self.add_client(ticker=bad, subaccount="sub9", email="x@y.com")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No clients on the desk yet", page)

    def test_weights_the_bot_would_refuse_are_refused_here(self):
        # These all parse as floats, so the box used to take them; the bot then
        # dropped the whole sub-account (or crashed its tick) and the VA — told
        # "Applied 1 client(s)" — had no idea the client was buying nothing.
        for bad in ("XRP:inf,XLM", "XRP:nan,XLM", "XRP:Infinity,XLM",
                    "XRP:1e400,XLM", "XRP:1e308,XLM", "XRP:-0.0,XLM"):
            self.add_client(ticker=bad, subaccount="sub9", email="x@y.com")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No clients on the desk yet", page)

    def test_every_ticker_the_ui_accepts_loads_in_the_bot(self):
        # The UI and the bot must agree exactly: a ticker the box takes but the
        # bot refuses disables that client without anyone being told.
        import autobuy

        env = {"SUB9_API_KEY": "k", "SUB9_API_SECRET": "s", "SUB9_API_PASSPHRASE": "p"}
        cfg_path = os.path.join(self.tmp.name, "config.json")
        for row_id, ticker in enumerate(
                ("btc", " eth ", "XRP,XLM,ADA", "xrp:3, xlm:2", "A:1e-9,B:1e9",
                 "XRP:0.5,XLM:0.25", "SOL:1000000", "X:1,Y:1"), start=1):
            self.add_client(ticker=ticker, subaccount="sub9", email="x@y.com")
            self.client.post("/apply")
            with mock.patch.dict(os.environ, env):
                cfg = autobuy.load_config(cfg_path)  # raises if nothing usable
            # Same coins, same order, same split (a lone leg's weight is moot,
            # so compare shares rather than raw weights).
            want, got = parse_ticker_field(ticker), autobuy.parse_basket(cfg["subaccounts"][0])
            self.assertEqual([c for c, _ in got], [c for c, _ in want], ticker)
            want_total, got_total = sum(w for _, w in want), sum(w for _, w in got)
            for (_, gw), (_, ww) in zip(got, want):
                self.assertAlmostEqual(gw / got_total, ww / want_total, places=12, msg=ticker)
            self.client.post("/delete/%d" % row_id, follow_redirects=True)

    def test_ui_weight_bounds_match_the_bot(self):
        import autobuy

        self.assertEqual((WEIGHT_MIN, WEIGHT_MAX), (autobuy.WEIGHT_MIN, autobuy.WEIGHT_MAX))


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_password_gate(self):
        with mock.patch.dict(os.environ, {"WEB_PASSWORD": "hunter2"}):
            app = create_app(data_dir=self.tmp.name)
            app.config["TESTING"] = True
            client = app.test_client()

            resp = client.get("/")
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login", resp.headers["Location"])

            resp = client.post("/login", data={"password": "wrong"})
            self.assertIn("Wrong password", resp.get_data(as_text=True))

            resp = client.post("/login", data={"password": "hunter2"}, follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)

    def test_no_password_means_open(self):
        os.environ.pop("WEB_PASSWORD", None)
        app = create_app(data_dir=self.tmp.name)
        app.config["TESTING"] = True
        self.assertEqual(app.test_client().get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
