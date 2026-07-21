"""Tests for multi-coin baskets: one sub-account spreading each arriving
deposit across several coins. All HTTP is mocked — no network, no credentials.

Run from the repo root:  python -m unittest tests.test_basket -v
"""

import json
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autobuy
from autobuy import SubAccountWorker, parse_basket

BASE = {
    "name": "sub1",
    "api_key": "k",
    "api_secret": "s",
    "api_passphrase": "p",
    "min_usdt_to_trigger": 5,
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def ok(data):
    return FakeResponse({"code": "00000", "msg": "success", "data": data})


class FakeHTTP:
    """Serves any symbol; records placed orders as (symbol, size)."""

    def __init__(self, usdt="100.0", min_trade="1", precision="4", fail_symbols=(),
                 no_info=()):
        self.usdt = usdt
        self.min_trade = min_trade
        self.precision = precision
        self.fail_symbols = set(fail_symbols)
        self.no_info = set(no_info)  # symbols whose metadata lookup errors
        self.orders = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        if "/spot/account/assets" in url:
            return ok([{"coin": "USDT", "available": self.usdt}])
        if "/spot/public/symbols" in url:
            symbol = url.split("symbol=")[1].split("&")[0]
            if symbol in self.no_info:
                return FakeResponse({"code": "40034", "msg": "symbol not exist"})
            return ok([{"symbol": symbol, "status": "online",
                        "minTradeUSDT": self.min_trade, "quotePrecision": self.precision}])
        if "/spot/trade/place-order" in url:
            body = json.loads(data)
            if body["symbol"] in self.fail_symbols:
                return FakeResponse({"code": "43001", "msg": "insufficient balance"})
            self.orders.append((body["symbol"], body["size"]))
            return ok({"orderId": str(1000 + len(self.orders))})
        if "/spot/trade/orderInfo" in url:
            return ok([{"status": "filled", "baseVolume": "1", "quoteVolume": "1"}])
        raise AssertionError("unexpected request: %s" % url)

    def sizes(self):
        return {sym: float(size) for sym, size in self.orders}


def patch_http(fake):
    return mock.patch("requests.Session.request", new=fake)


class TestParseBasket(unittest.TestCase):
    def test_single_buy_coin_is_a_one_leg_basket(self):
        self.assertEqual(parse_basket({"buy_coin": "xlm"}), [("XLM", 1.0)])

    def test_list_of_strings_is_equal_weighted(self):
        self.assertEqual(parse_basket({"basket": ["xrp", "XLM"]}),
                         [("XRP", 1.0), ("XLM", 1.0)])

    def test_dicts_carry_weights(self):
        self.assertEqual(parse_basket({"basket": [{"coin": "XRP", "weight": 3},
                                                  {"coin": "XLM", "weight": 2}]}),
                         [("XRP", 3.0), ("XLM", 2.0)])

    def test_basket_wins_over_buy_coin(self):
        self.assertEqual(parse_basket({"buy_coin": "BTC", "basket": ["XRP"]}), [("XRP", 1.0)])

    def test_duplicate_coin_rejected(self):
        with self.assertRaisesRegex(ValueError, "twice"):
            parse_basket({"basket": ["XRP", "xrp"]})

    def test_non_positive_and_nonsense_weights_rejected(self):
        for bad in (0, -1, "abc", float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                parse_basket({"basket": [{"coin": "XRP", "weight": bad}]})

    def test_extreme_but_finite_weights_rejected(self):
        # 1e308 is finite and positive, so it used to pass — and then
        # budget*weight overflowed to inf inside tick(), killing every leg.
        for bad in (1e308, 1e-308, 1e10, 1e-10):
            with self.assertRaisesRegex(ValueError, "between"):
                parse_basket({"basket": [{"coin": "XRP", "weight": bad},
                                         {"coin": "XLM", "weight": 1}]})

    def test_weights_at_the_bounds_are_accepted(self):
        self.assertEqual(parse_basket({"basket": [{"coin": "XRP", "weight": autobuy.WEIGHT_MAX},
                                                  {"coin": "XLM", "weight": autobuy.WEIGHT_MIN}]}),
                         [("XRP", 1e9), ("XLM", 1e-9)])

    def test_empty_or_malformed_rejected(self):
        for bad in ({"basket": []}, {"basket": "XRP"}, {"basket": [{"weight": 1}]},
                    {"basket": [123]}):
            with self.assertRaises((ValueError, KeyError)):
                parse_basket(bad)


class TestBasketTick(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(autobuy.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_even_split_across_three_coins(self):
        fake = FakeHTTP(usdt="90.0")
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA"])).tick()
        self.assertEqual(fake.sizes(),
                         {"XRPUSDT": 30.0, "XLMUSDT": 30.0, "ADAUSDT": 30.0})

    def test_weighted_split(self):
        fake = FakeHTTP(usdt="100.0")
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=[{"coin": "XRP", "weight": 3},
                                                {"coin": "XLM", "weight": 1}])).tick()
        self.assertEqual(fake.sizes(), {"XRPUSDT": 75.0, "XLMUSDT": 25.0})

    def test_reserve_and_cap_apply_to_the_whole_basket(self):
        fake = FakeHTTP(usdt="100.0")
        cfg = dict(BASE, basket=["XRP", "XLM"], reserve_usdt=20, max_usdt_per_order=40)
        with patch_http(fake):
            SubAccountWorker(cfg).tick()
        # spendable 80, capped to 40 total, split evenly
        self.assertEqual(fake.sizes(), {"XRPUSDT": 20.0, "XLMUSDT": 20.0})

    def test_never_spends_more_than_the_budget(self):
        fake = FakeHTTP(usdt="10.0")
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA"])).tick()
        self.assertLessEqual(sum(fake.sizes().values()), 10.0)

    def test_legs_below_the_exchange_minimum_are_skipped_not_rounded_up(self):
        # 6 USDT across 4 coins = 1.5 each (ok), but with min 2.0 all fail.
        fake = FakeHTTP(usdt="6.0", min_trade="2")
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA", "SOL"])).tick()
        self.assertEqual(fake.orders, [])

    def test_partial_basket_still_buys_the_legs_that_qualify(self):
        # 10 USDT, XRP weighted 9/10 -> 9.0 (>=2), XLM 1/10 -> 1.0 (<2, skipped)
        fake = FakeHTTP(usdt="10.0", min_trade="2")
        cfg = dict(BASE, basket=[{"coin": "XRP", "weight": 9}, {"coin": "XLM", "weight": 1}])
        with patch_http(fake):
            SubAccountWorker(cfg).tick()
        self.assertEqual(fake.sizes(), {"XRPUSDT": 9.0})

    def test_stalled_basket_logs_once_not_every_cycle(self):
        # Balance stays above the trigger but no leg clears its minimum, so
        # this path repeats forever; it must not spam the log every 30s.
        fake = FakeHTTP(usdt="6.0", min_trade="2")
        worker = SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA", "SOL"]))
        with patch_http(fake):
            with self.assertLogs("autobuy", level="INFO") as first:
                worker.tick()
            worker.tick()  # unchanged circumstances -> silent
            with self.assertLogs("autobuy", level="INFO") as third:
                fake.usdt = "7.0"  # circumstances changed -> speaks again
                worker.tick()
        self.assertTrue(any("needs about" in m for m in first.output))
        self.assertTrue(any("needs about" in m for m in third.output))
        self.assertEqual(fake.orders, [])

    def test_stall_state_clears_after_a_successful_cycle(self):
        # 6 USDT over 2 coins = 3.00 each, below the 4.00 minimum -> stalled.
        fake = FakeHTTP(usdt="6.0", min_trade="4")
        worker = SubAccountWorker(dict(BASE, basket=["XRP", "XLM"]))
        with patch_http(fake):
            worker.tick()
            self.assertIsNotNone(worker._last_stall)
            fake.usdt = "90.0"  # 45.00 each -> both clear
            worker.tick()
        self.assertIsNone(worker._last_stall)
        self.assertEqual(set(fake.sizes()), {"XRPUSDT", "XLMUSDT"})

    def test_needed_hint_uses_the_smallest_weighted_leg(self):
        # 2 USDT, weights 9:1, minimum 2.00 -> XRP gets 1.80 and XLM 0.20, so
        # both are skipped. The binding constraint is the *smallest* leg:
        # XLM needs 2.00, which is a tenth of the basket, so 20.00 in total.
        fake = FakeHTTP(usdt="2.0", min_trade="2")
        cfg = dict(BASE, min_usdt_to_trigger=1,
                   basket=[{"coin": "XRP", "weight": 9}, {"coin": "XLM", "weight": 1}])
        with patch_http(fake):
            with self.assertLogs("autobuy", level="INFO") as logs:
                SubAccountWorker(cfg).tick()
        self.assertEqual(fake.orders, [])
        self.assertTrue(any("about 20.00 USDT" in m for m in logs.output), logs.output)

    def test_below_trigger_buys_nothing(self):
        fake = FakeHTTP(usdt="3.0")
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM"])).tick()
        self.assertEqual(fake.orders, [])

    def test_dry_run_places_no_orders(self):
        fake = FakeHTTP()
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM"]), dry_run=True).tick()
        self.assertEqual(fake.orders, [])

    def test_one_failing_leg_does_not_abort_the_others(self):
        fake = FakeHTTP(usdt="90.0", fail_symbols={"XLMUSDT"})
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA"])).tick()
        self.assertEqual(set(fake.sizes()), {"XRPUSDT", "ADAUSDT"})

    def test_no_random_configuration_overspends_the_budget(self):
        # Property check over many shapes: whatever the weights and precision,
        # the legs together never send the exchange more than the budget.
        rnd = random.Random(20260721)
        for _ in range(200):
            weights = [rnd.choice([rnd.uniform(1e-6, 1e6), float(rnd.randint(1, 9))])
                       for _ in range(rnd.randint(2, 8))]
            basket = [{"coin": "C%d" % i, "weight": w} for i, w in enumerate(weights)]
            budget = round(rnd.uniform(1, 100000), rnd.randint(0, 8))
            fake = FakeHTTP(usdt=repr(budget), min_trade="0",
                            precision=str(rnd.randint(0, 8)))
            with patch_http(fake):
                SubAccountWorker(dict(BASE, basket=basket, min_usdt_to_trigger=0)).tick()
            # The only slack allowed is the last-bit float noise of flooring a
            # balance that is itself the nearest double to a decimal string.
            self.assertLessEqual(sum(fake.sizes().values()), budget + budget * 1e-12 + 1e-9,
                                 "overspent: weights=%r budget=%r sizes=%r"
                                 % (weights, budget, fake.sizes()))

    def test_unfetchable_symbol_does_not_stop_the_other_legs(self):
        # A delisted/unknown coin must cost only its own leg, exactly like a
        # rejected order does — not every sibling, every cycle, forever.
        fake = FakeHTTP(usdt="90.0", no_info={"XLMUSDT"})
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA"])).tick()
        self.assertEqual(set(fake.sizes()), {"XRPUSDT", "ADAUSDT"})

    def test_stalled_basket_blames_the_cap_when_the_cap_is_what_binds(self):
        # Plenty spendable, but max_usdt_per_order holds the budget under the
        # legs' minimums — telling the user to add USDT would be a lie.
        fake = FakeHTTP(usdt="100000.0", min_trade="4")
        cfg = dict(BASE, basket=["XRP", "XLM"], max_usdt_per_order=5)
        with patch_http(fake):
            with self.assertLogs("autobuy", level="INFO") as logs:
                SubAccountWorker(cfg).tick()
        self.assertEqual(fake.orders, [])
        self.assertTrue(any("max_usdt_per_order caps it at 5.00" in m for m in logs.output),
                        logs.output)
        self.assertFalse(any("USDT spendable to place every leg" in m for m in logs.output),
                         logs.output)

    def test_single_coin_failure_still_propagates(self):
        # The pre-basket contract: a lone worker's error reaches the main loop.
        fake = FakeHTTP(usdt="90.0", fail_symbols={"XLMUSDT"})
        with patch_http(fake):
            with self.assertRaises(autobuy.ExchangeAPIError):
                SubAccountWorker(dict(BASE, buy_coin="XLM")).tick()

    def test_amounts_are_floored_to_quote_precision(self):
        fake = FakeHTTP(usdt="100.0", precision="2")
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA"])).tick()
        for size in fake.sizes().values():
            self.assertEqual(round(size, 2), size)
        self.assertLessEqual(sum(fake.sizes().values()), 100.0)

    def test_symbol_metadata_is_cached_per_symbol(self):
        # One cache entry per leg, fetched once, and each leg formatted to its
        # own precision — a shared entry would size orders with the wrong one.
        fetched = []
        fake = FakeHTTP(usdt="90.0")
        precisions = {"XRPUSDT": "4", "XLMUSDT": "2", "ADAUSDT": "0"}
        inner = fake.__call__

        def spy(method, url, **kw):
            if "/spot/public/symbols" in url:
                symbol = url.split("symbol=")[1].split("&")[0]
                fetched.append(symbol)
                fake.precision = precisions[symbol]
            return inner(method, url, **kw)

        with patch_http(lambda s, method, url, **kw: spy(method, url, **kw)):
            worker = SubAccountWorker(dict(BASE, basket=["XRP", "XLM", "ADA"]))
            worker.tick()
            worker.tick()
        self.assertEqual(sorted(fetched), ["ADAUSDT", "XLMUSDT", "XRPUSDT"])
        self.assertEqual(len(worker._symbol_info), 3)
        self.assertEqual(dict(fake.orders[:3]),
                         {"XRPUSDT": "30.0000", "XLMUSDT": "30.00", "ADAUSDT": "30"})

    def test_disabled_worker_does_nothing(self):
        fake = FakeHTTP()
        with patch_http(fake):
            SubAccountWorker(dict(BASE, basket=["XRP", "XLM"], enabled=False)).tick()
        self.assertEqual(fake.orders, [])

    def test_describe_reports_the_split(self):
        fake = FakeHTTP()
        with patch_http(fake):
            w = SubAccountWorker(dict(BASE, basket=[{"coin": "XRP", "weight": 3},
                                                    {"coin": "XLM", "weight": 1}]))
        self.assertTrue(w.is_basket)
        self.assertIn("XRP 75%", w.describe())
        self.assertIn("XLM 25%", w.describe())


class TestSingleCoinUnchanged(unittest.TestCase):
    """The live bot runs a single-coin worker; it must behave exactly as before."""

    def setUp(self):
        patcher = mock.patch.object(autobuy.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_coin_spends_the_whole_spendable_balance(self):
        fake = FakeHTTP(usdt="123.456789")
        with patch_http(fake):
            w = SubAccountWorker(dict(BASE, buy_coin="XLM"))
            w.tick()
        self.assertFalse(w.is_basket)
        self.assertEqual(w.symbol, "XLMUSDT")
        self.assertEqual(w.describe(), "XLMUSDT")
        self.assertEqual(fake.orders, [("XLMUSDT", "123.4567")])

    def test_below_minimum_logs_the_pre_basket_line_every_cycle(self):
        # Word for word what the live bot logged before baskets existed, and
        # once per cycle — no "basket" wording, no once-only suppression.
        fake = FakeHTTP(usdt="100.0", min_trade="5")
        cfg = dict(BASE, buy_coin="XLM", min_usdt_to_trigger=0.5, max_usdt_per_order=3)
        with patch_http(fake):
            worker = SubAccountWorker(cfg)
            with self.assertLogs("autobuy", level="INFO") as logs:
                worker.tick()
                worker.tick()
        self.assertEqual(
            [m.split(":", 2)[2] for m in logs.output],
            ["[sub1] 3.0000 USDT spendable but symbol minimum is 5.00 — waiting"] * 2)
        self.assertEqual(fake.orders, [])

    def test_symbol_metadata_failure_still_propagates(self):
        # The main loop relies on catching this; only baskets swallow it.
        fake = FakeHTTP(usdt="90.0", no_info={"XLMUSDT"})
        with patch_http(fake):
            with self.assertRaises(autobuy.ExchangeAPIError):
                SubAccountWorker(dict(BASE, buy_coin="XLM")).tick()

    def test_order_failure_is_reported_once_by_the_caller_not_twice(self):
        # Pre-basket, a lone worker's error was logged only by the main loop.
        fake = FakeHTTP(usdt="90.0", fail_symbols={"XLMUSDT"})
        with patch_http(fake):
            with self.assertLogs("autobuy", level="INFO") as logs:
                with self.assertRaises(autobuy.ExchangeAPIError):
                    SubAccountWorker(dict(BASE, buy_coin="XLM")).tick()
        self.assertFalse(any("leg failed" in m for m in logs.output), logs.output)


class TestConfigValidation(unittest.TestCase):
    def _load(self, sub):
        import tempfile
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"subaccounts": [sub]}, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return autobuy.load_config(fh.name)

    def test_basket_config_loads(self):
        cfg = self._load(dict(BASE, basket=["XRP", "XLM"]))
        self.assertEqual(cfg["subaccounts"][0]["basket"], ["XRP", "XLM"])

    def test_entry_with_neither_target_is_skipped(self):
        with self.assertRaisesRegex(ValueError, "buy_coin"):
            self._load(dict(BASE))

    def test_malformed_basket_is_skipped_with_its_reason(self):
        with self.assertRaisesRegex(ValueError, "twice"):
            self._load(dict(BASE, basket=["XRP", "XRP"]))


if __name__ == "__main__":
    unittest.main()
