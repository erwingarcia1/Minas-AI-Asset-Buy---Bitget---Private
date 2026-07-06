"""Tests for the admin web UI. Uses Flask's test client — no server, no network."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.app import create_app, env_prefix, key_vars

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
        self.assertIn("No clients yet", page)

    def test_edit_and_delete(self):
        self.add_client()
        self.client.post("/edit/1", data=dict(CLIENT, name="Janet Doe", ticker="ETH"))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Janet Doe", page)
        self.assertIn("ETH", page)
        self.client.post("/delete/1")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No clients yet", page)

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
