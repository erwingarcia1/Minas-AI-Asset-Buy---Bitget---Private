#!/usr/bin/env python3
"""Multi-exchange sub-account USDT auto-buy bot.

Polls the spot USDT balance of each configured sub-account and, whenever the
available balance exceeds the configured threshold, places a spot market buy
for that sub-account's target coin.

Each sub-account entry in config.json carries its own exchange, API
credentials (exchanges scope keys to the account they are created under —
create the key from the parent account's sub-account management page) plus
the coin to buy. Supported exchanges: Bitget, Bybit, OKX, Binance.

Usage:
    python autobuy.py                 # run with config.json
    python autobuy.py --config x.json # alternate config file
    python autobuy.py --once          # single pass, then exit
    python autobuy.py --dry-run       # log what would be bought, place no orders
"""

import argparse
import importlib
import json
import logging
import math
import os
import re
import sys
import time
import uuid

from exchange_errors import ExchangeAPIError

log = logging.getLogger("autobuy")

QUOTE_COIN = "USDT"
SYMBOL_CACHE_TTL = 3600  # seconds between refreshes of symbol metadata

# Accepted range for a basket weight. Anything outside it is either nonsense
# (0, NaN, inf) or a ratio so lopsided that the small leg can never trade;
# the bound also keeps the share arithmetic away from float overflow.
WEIGHT_MIN, WEIGHT_MAX = 1e-9, 1e9

# exchange name (lowercase) -> (module, class). Imported lazily so a missing
# or broken client module only affects configs that actually use that venue.
EXCHANGE_CLIENTS = {
    "bitget": ("bitget_client", "BitgetClient"),
    "bybit": ("bybit_client", "BybitClient"),
    "okx": ("okx_client", "OKXClient"),
    "binance": ("binance_client", "BinanceClient"),
}

# Venues whose API keys carry a passphrase; the rest need only key + secret.
PASSPHRASE_EXCHANGES = {"bitget", "okx"}

EXCHANGE_LABELS = {"bitget": "Bitget", "bybit": "Bybit", "okx": "OKX", "binance": "Binance"}


def make_client(exchange, api_key="", api_secret="", passphrase=""):
    """Instantiate the REST client for `exchange` (lowercase, validated)."""
    module_name, class_name = EXCHANGE_CLIENTS[exchange]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)(
        api_key=api_key, api_secret=api_secret, passphrase=passphrase)


def parse_basket(cfg):
    """Normalize a sub-account's target(s) into [(COIN, weight), ...].

    Accepts either the original single-coin form ("buy_coin": "XLM") or a
    basket ("basket": [{"coin": "XRP", "weight": 3}, ...]). A bare string in
    the basket list means weight 1. A single-coin config is exactly a
    one-leg basket, so both forms share one code path.
    """
    raw = cfg.get("basket")
    if not raw:
        return [(cfg["buy_coin"].strip().upper(), 1.0)]
    if not isinstance(raw, list):
        raise ValueError("'basket' must be a list")

    legs, seen = [], set()
    for entry in raw:
        if isinstance(entry, str):
            coin, weight = entry, 1.0
        elif isinstance(entry, dict):
            coin, weight = entry.get("coin", ""), entry.get("weight", 1)
        else:
            raise ValueError(f"bad basket entry: {entry!r}")
        coin = str(coin).strip().upper()
        if not coin:
            raise ValueError("basket entry is missing 'coin'")
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            raise ValueError(f"basket weight for {coin} is not a number: {weight!r}")
        # Bounded, not merely positive: NaN and both infinities fail this test,
        # and so do weights so extreme that budget*weight/total overflows to
        # inf/NaN and kills the tick. Weights are ratios, not amounts.
        if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
            raise ValueError(f"basket weight for {coin} must be a number between "
                             f"{WEIGHT_MIN:g} and {WEIGHT_MAX:g}")
        if coin in seen:
            raise ValueError(f"{coin} appears twice in the basket")
        seen.add(coin)
        legs.append((coin, weight))

    if not legs:
        raise ValueError("'basket' is empty")
    return legs


class SubAccountWorker:
    """Owns one sub-account: its API client, target coins, and buy rules."""

    def __init__(self, cfg, dry_run=False):
        self.name = cfg["name"]
        self.exchange = str(cfg.get("exchange", "bitget")).lower()
        self.min_usdt = float(cfg.get("min_usdt_to_trigger", 5))
        self.reserve_usdt = float(cfg.get("reserve_usdt", 0))
        # Caps the TOTAL deployed per cycle. For a single-coin worker that is
        # the one order, so the meaning is unchanged from before baskets.
        self.max_usdt_per_order = float(cfg.get("max_usdt_per_order", 0)) or None
        self.enabled = bool(cfg.get("enabled", True))
        self.dry_run = dry_run
        self.client = make_client(
            self.exchange,
            api_key=cfg["api_key"],
            api_secret=cfg["api_secret"],
            passphrase=cfg.get("api_passphrase", ""),
        )
        self.legs = parse_basket(cfg)
        self.coin = self.legs[0][0]  # kept for logs and single-coin callers
        self.symbols = {coin: self.client.spot_symbol(coin, QUOTE_COIN)
                        for coin, _ in self.legs}
        self.symbol = self.symbols[self.coin]
        self._symbol_info = {}
        self._symbol_info_at = {}
        self._last_stall = None  # suppresses repeat "nothing is buyable" logs

    @property
    def is_basket(self):
        return len(self.legs) > 1

    def describe(self):
        """One-line summary of what this worker buys, for the startup log."""
        if not self.is_basket:
            return self.symbol
        total = sum(w for _, w in self.legs)
        return "basket of %d: %s" % (
            len(self.legs),
            ", ".join("%s %.0f%%" % (c, 100 * w / total) for c, w in self.legs),
        )

    # ----------------------------------------------------------- symbol meta

    def symbol_info(self, coin=None):
        coin = coin or self.coin
        symbol = self.symbols[coin]
        cached_at = self._symbol_info_at.get(symbol, 0.0)
        if symbol not in self._symbol_info or time.time() - cached_at > SYMBOL_CACHE_TTL:
            info = self.client.get_symbol_info(symbol)
            self._symbol_info[symbol] = info
            self._symbol_info_at[symbol] = time.time()
            if info.get("status") != "online":
                log.warning("[%s] symbol %s status is %r, orders may fail",
                            self.name, symbol, info.get("status"))
        return self._symbol_info[symbol]

    def _floor_to_precision(self, amount, precision):
        factor = 10 ** precision
        return math.floor(amount * factor) / factor

    # ------------------------------------------------------------------ tick

    def tick(self):
        """One poll cycle: check balance, buy if above threshold."""
        if not self.enabled:
            return

        available = self.client.get_spot_available(QUOTE_COIN)
        spendable = available - self.reserve_usdt
        if spendable < self.min_usdt:
            log.debug("[%s] %.4f USDT available (%.4f spendable) — below %.2f trigger",
                      self.name, available, max(spendable, 0), self.min_usdt)
            return

        budget = spendable
        if self.max_usdt_per_order:
            budget = min(budget, self.max_usdt_per_order)

        total_weight = sum(w for _, w in self.legs)
        planned, skipped = [], []
        for coin, weight in self.legs:
            try:
                info = self.symbol_info(coin)
            except ExchangeAPIError as exc:
                # One unusable symbol must not hold the other legs hostage.
                # A single-coin worker still propagates, as it always did.
                if not self.is_basket:
                    raise
                log.error("[%s] %s metadata unavailable, skipping this leg: %s",
                          self.name, coin, exc)
                continue
            min_trade = float(info.get("minTradeUSDT", 1))
            quote_precision = int(info.get("quotePrecision", 4))
            amount = self._floor_to_precision(budget * weight / total_weight, quote_precision)
            if amount < min_trade:
                skipped.append((coin, amount, min_trade, weight))
                continue
            planned.append((coin, amount, f"{amount:.{quote_precision}f}"))

        if skipped and not planned:
            if not self.is_basket:
                # Word for word (and every cycle) as before baskets existed:
                # the live single-coin bot is watched through this line.
                _, amount, min_trade, _ = skipped[0]
                log.info("[%s] %.4f USDT spendable but symbol minimum is %.2f — waiting",
                         self.name, amount, min_trade)
                return
            # Nothing is buyable. The balance stays above the trigger, so this
            # branch would repeat every poll cycle — log it once per change of
            # circumstances instead of every 30 seconds, and say what it would
            # actually take rather than only complaining per leg.
            needed = max(min_trade * total_weight / weight
                         for _, _, min_trade, weight in skipped)
            state = ("stalled", round(budget, 4), len(skipped))
            if state != self._last_stall:
                self._last_stall = state
                for coin, amount, min_trade, _ in skipped:
                    log.info("[%s] %s share is %.4f USDT but the symbol minimum is %.2f",
                             self.name, coin, amount, min_trade)
                if self.max_usdt_per_order and needed > self.max_usdt_per_order:
                    # More USDT would not help: the cap, not the balance, binds.
                    log.info("[%s] no leg meets its minimum with %.4f USDT — this basket needs "
                             "about %.2f USDT per cycle but max_usdt_per_order caps it at "
                             "%.2f; raise the cap or drop legs",
                             self.name, budget, needed, self.max_usdt_per_order)
                else:
                    log.info("[%s] no leg meets its minimum with %.4f USDT — this basket needs "
                             "about %.2f USDT spendable to place every leg",
                             self.name, budget, needed)
            return
        self._last_stall = None

        for coin, amount, min_trade, _ in skipped:
            log.info("[%s] %s share is %.4f USDT but the symbol minimum is %.2f — skipping this leg",
                     self.name, coin, amount, min_trade)

        for coin, amount, size in planned:
            try:
                self._buy_leg(coin, size)
            except ExchangeAPIError as exc:
                # One bad leg must not abandon the rest of the basket. A lone
                # worker re-raises untouched — the main loop reports it.
                if not self.is_basket:
                    raise
                log.error("[%s] %s leg failed: %s", self.name, coin, exc)

    def _buy_leg(self, coin, size):
        """Place (or simulate) one market buy for one coin of the basket."""
        symbol = self.symbols[coin]
        if self.dry_run:
            log.info("[%s] DRY RUN: would market-buy %s with %s USDT", self.name, symbol, size)
            return

        client_oid = f"autobuy-{uuid.uuid4().hex[:20]}"
        log.info("[%s] placing market buy: %s for %s USDT (clientOid=%s)",
                 self.name, symbol, size, client_oid)
        result = self.client.place_market_buy(symbol, size, client_oid=client_oid)
        order_id = (result or {}).get("orderId")
        log.info("[%s] order accepted: orderId=%s", self.name, order_id)

        # Best-effort fill confirmation for the logs; the order itself is done.
        if order_id:
            time.sleep(1.5)
            try:
                order = self.client.get_order_info(symbol, order_id)
                if order:
                    log.info("[%s] order %s status=%s filled=%s %s for %s USDT",
                             self.name, order_id, order.get("status"),
                             order.get("baseVolume"), coin, order.get("quoteVolume"))
            except ExchangeAPIError as exc:
                log.warning("[%s] could not fetch fill info for %s: %s", self.name, order_id, exc)


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value):
    """Expand ${VAR} references from the environment in config string values.

    Lets deployments keep secrets out of the config file entirely
    (e.g. "api_key": "${SUB1_API_KEY}"). Unset variables are an error.
    """
    if isinstance(value, str):
        def repl(match):
            var = match.group(1)
            if var not in os.environ:
                raise ValueError(f"config references ${{{var}}} but it is not set in the environment")
            return os.environ[var]
        return _ENV_REF.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def load_config(path):
    """Load and validate config. Sub-account entries that are incomplete or
    reference unset env vars are skipped with a warning (so one client added
    via the web UI before their API keys exist doesn't stop the others);
    raises ValueError only when no usable entry remains.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    subs_raw = raw.get("subaccounts", [])
    if not isinstance(subs_raw, list) or not subs_raw:
        raise ValueError("config has no 'subaccounts' entries")

    cfg = {k: _expand_env(v) for k, v in raw.items() if k != "subaccounts"}

    required = ("name", "api_key", "api_secret")
    valid, skipped = [], []
    for i, sub in enumerate(subs_raw):
        label = sub.get("name") or f"subaccounts[{i}]"
        try:
            sub = _expand_env(sub)
            exchange = str(sub.get("exchange", "bitget")).lower()
            if exchange not in EXCHANGE_CLIENTS:
                raise ValueError(f"unknown exchange {exchange!r} "
                                 f"(supported: {', '.join(sorted(EXCHANGE_CLIENTS))})")
            sub["exchange"] = exchange
            missing = [k for k in required if not sub.get(k)]
            # Either form of target is fine, but one of them must be present
            # and well-formed; parse_basket raises with the specific reason.
            if not sub.get("buy_coin") and not sub.get("basket"):
                missing.append("buy_coin")
            else:
                parse_basket(sub)
            # Only Bitget and OKX keys carry a passphrase.
            if exchange in PASSPHRASE_EXCHANGES and not sub.get("api_passphrase"):
                missing.append("api_passphrase")
            if missing:
                raise ValueError(f"missing fields: {', '.join(missing)}")
            valid.append(sub)
        except ValueError as exc:
            skipped.append((label, str(exc)))

    for label, why in skipped:
        log.warning("skipping sub-account %r: %s", label, why)
    if not valid:
        raise ValueError(
            "no usable sub-account entries — " + "; ".join(f"{l}: {w}" for l, w in skipped)
        )
    cfg["subaccounts"] = valid
    return cfg


def selftest():
    """Verify this machine can run the bot — no API keys or config needed.

    Checks every supported exchange's public-API reachability, latency, and
    market data, plus local clock skew against the first reachable venue
    (signed requests are rejected if the clock is off). Returns an exit code.
    """
    failures = 0
    reachable = 0
    clock_checked = False

    for exchange in sorted(EXCHANGE_CLIENTS):
        label = EXCHANGE_LABELS[exchange]
        try:
            client = make_client(exchange)
        except Exception as exc:  # missing/broken client module
            log.error("FAIL  %s client unavailable: %s", label, exc)
            failures += 1
            continue

        try:
            t0 = time.time()
            server_ms = client.get_server_time()
            latency_ms = (time.time() - t0) * 1000
            log.info("PASS  %s API reachable (latency %.0f ms)", label, latency_ms)
            reachable += 1
            if not clock_checked:
                clock_checked = True
                skew_ms = abs(server_ms - int((t0 + (time.time() - t0) / 2) * 1000))
                if skew_ms < 2000:
                    log.info("PASS  clock skew vs %s: %d ms", label, skew_ms)
                else:
                    log.error("FAIL  clock skew vs %s is %d ms (>2s) — fix NTP: "
                              "sudo timedatectl set-ntp true", label, skew_ms)
                    failures += 1
        except ExchangeAPIError as exc:
            log.error("FAIL  cannot reach %s API: %s", label, exc)
            failures += 1
            continue

        try:
            symbol = client.spot_symbol("BTC", QUOTE_COIN)
            info = client.get_symbol_info(symbol)
            price = client.get_ticker_price(symbol)
            log.info("PASS  %s market data OK (%s status=%s, last price %.2f USDT)",
                     label, symbol, info.get("status"), price)
        except ExchangeAPIError as exc:
            log.error("FAIL  %s market data: %s", label, exc)
            failures += 1

    if not reachable:
        log.error("      (mainland-China regions and some networks block exchange APIs)")
    if failures:
        log.error("selftest FAILED (%d problem%s)", failures, "s" if failures > 1 else "")
        return 1
    log.info("selftest PASSED — this machine is ready. Next: add API keys, then --once --dry-run")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Multi-exchange sub-account USDT auto-buy bot (Bitget, Bybit, OKX, Binance)")
    parser.add_argument("--config", default=os.environ.get("AUTOBUY_CONFIG", "config.json"))
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="log intended buys without placing orders")
    parser.add_argument("--selftest", action="store_true",
                        help="check exchange connectivity and clock sync (no config/keys needed), then exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.selftest:
        sys.exit(selftest())

    poll_seconds = 30.0
    workers = []
    cfg_mtime = object()  # sentinel != any real mtime, forces initial load

    while True:
        # Hot-reload: pick up config changes (e.g. applied from the web UI)
        # without a restart. On a bad config, keep the previous workers.
        try:
            mtime = os.stat(args.config).st_mtime
        except OSError:
            mtime = None
        if mtime != cfg_mtime:
            first_load = cfg_mtime.__class__ is object
            cfg_mtime = mtime
            try:
                cfg = load_config(args.config)
                poll_seconds = float(cfg.get("poll_seconds", 30))
                workers = [SubAccountWorker(sub, dry_run=args.dry_run) for sub in cfg["subaccounts"]]
                log.info("%s config: %d sub-account(s), polling every %.0fs%s",
                         "loaded" if first_load else "reloaded", len(workers), poll_seconds,
                         " (dry run)" if args.dry_run else "")
                for w in workers:
                    log.info("  - %s: buys %s when USDT > %.2f%s", w.name, w.describe(), w.min_usdt,
                             "" if w.enabled else " [DISABLED]")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                log.error("config error: %s", exc)
                if args.once:
                    sys.exit(1)
                if first_load or not workers:
                    workers = []
                    log.info("waiting for a valid %s (watching for changes)...", args.config)
                else:
                    log.warning("keeping previous config (%d sub-account(s))", len(workers))

        for worker in workers:
            try:
                worker.tick()
            except ExchangeAPIError as exc:
                log.error("[%s] %s API error: %s", worker.name, exc.exchange, exc)
            except Exception:
                log.exception("[%s] unexpected error", worker.name)
        if args.once:
            break
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
