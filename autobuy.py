#!/usr/bin/env python3
"""Bitget sub-account USDT auto-buy bot.

Polls the spot USDT balance of each configured sub-account and, whenever the
available balance exceeds the configured threshold, places a spot market buy
for that sub-account's target coin.

Each sub-account entry in config.json carries its own API credentials (Bitget
requires the sub-account's own API key to trade inside it — create one from
the parent account under "Sub-account management") plus the coin to buy.

Usage:
    python autobuy.py                 # run with config.json
    python autobuy.py --config x.json # alternate config file
    python autobuy.py --once          # single pass, then exit
    python autobuy.py --dry-run       # log what would be bought, place no orders
"""

import argparse
import json
import logging
import math
import os
import sys
import time
import uuid

from bitget_client import BitgetAPIError, BitgetClient

log = logging.getLogger("autobuy")

QUOTE_COIN = "USDT"
SYMBOL_CACHE_TTL = 3600  # seconds between refreshes of symbol metadata


class SubAccountWorker:
    """Owns one sub-account: its API client, target symbol, and buy rules."""

    def __init__(self, cfg, dry_run=False):
        self.name = cfg["name"]
        self.coin = cfg["buy_coin"].upper()
        self.symbol = f"{self.coin}{QUOTE_COIN}"
        self.min_usdt = float(cfg.get("min_usdt_to_trigger", 5))
        self.reserve_usdt = float(cfg.get("reserve_usdt", 0))
        self.max_usdt_per_order = float(cfg.get("max_usdt_per_order", 0)) or None
        self.enabled = bool(cfg.get("enabled", True))
        self.dry_run = dry_run
        self.client = BitgetClient(
            api_key=cfg["api_key"],
            api_secret=cfg["api_secret"],
            passphrase=cfg["api_passphrase"],
        )
        self._symbol_info = None
        self._symbol_info_at = 0.0

    # ----------------------------------------------------------- symbol meta

    def symbol_info(self):
        if self._symbol_info is None or time.time() - self._symbol_info_at > SYMBOL_CACHE_TTL:
            self._symbol_info = self.client.get_symbol_info(self.symbol)
            self._symbol_info_at = time.time()
            if self._symbol_info.get("status") != "online":
                log.warning("[%s] symbol %s status is %r, orders may fail",
                            self.name, self.symbol, self._symbol_info.get("status"))
        return self._symbol_info

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

        info = self.symbol_info()
        min_trade = float(info.get("minTradeUSDT", 1))
        quote_precision = int(info.get("quotePrecision", 4))

        amount = spendable
        if self.max_usdt_per_order:
            amount = min(amount, self.max_usdt_per_order)
        amount = self._floor_to_precision(amount, quote_precision)
        size = f"{amount:.{quote_precision}f}"

        if amount < min_trade:
            log.info("[%s] %.4f USDT spendable but symbol minimum is %.2f — waiting",
                     self.name, amount, min_trade)
            return

        if self.dry_run:
            log.info("[%s] DRY RUN: would market-buy %s with %s USDT", self.name, self.symbol, size)
            return

        client_oid = f"autobuy-{uuid.uuid4().hex[:20]}"
        log.info("[%s] placing market buy: %s for %s USDT (clientOid=%s)",
                 self.name, self.symbol, size, client_oid)
        result = self.client.place_market_buy(self.symbol, size, client_oid=client_oid)
        order_id = (result or {}).get("orderId")
        log.info("[%s] order accepted: orderId=%s", self.name, order_id)

        # Best-effort fill confirmation for the logs; the order itself is done.
        if order_id:
            time.sleep(1.5)
            try:
                order = self.client.get_order_info(order_id)
                if order:
                    log.info("[%s] order %s status=%s filled=%s %s for %s USDT",
                             self.name, order_id, order.get("status"),
                             order.get("baseVolume"), self.coin, order.get("quoteVolume"))
            except BitgetAPIError as exc:
                log.warning("[%s] could not fetch fill info for %s: %s", self.name, order_id, exc)


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    subs = cfg.get("subaccounts", [])
    if not subs:
        raise ValueError("config has no 'subaccounts' entries")

    required = ("name", "api_key", "api_secret", "api_passphrase", "buy_coin")
    for i, sub in enumerate(subs):
        missing = [k for k in required if not sub.get(k)]
        if missing:
            raise ValueError(f"subaccounts[{i}] ({sub.get('name', '?')}) missing fields: {', '.join(missing)}")
    return cfg


def main():
    parser = argparse.ArgumentParser(description="Bitget sub-account USDT auto-buy bot")
    parser.add_argument("--config", default=os.environ.get("AUTOBUY_CONFIG", "config.json"))
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="log intended buys without placing orders")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.error("config error: %s", exc)
        sys.exit(1)

    poll_seconds = float(cfg.get("poll_seconds", 30))
    workers = [SubAccountWorker(sub, dry_run=args.dry_run) for sub in cfg["subaccounts"]]
    log.info("watching %d sub-account(s), polling every %.0fs%s",
             len(workers), poll_seconds, " (dry run)" if args.dry_run else "")
    for w in workers:
        log.info("  - %s: buys %s when USDT > %.2f%s", w.name, w.symbol, w.min_usdt,
                 "" if w.enabled else " [DISABLED]")

    while True:
        for worker in workers:
            try:
                worker.tick()
            except BitgetAPIError as exc:
                log.error("[%s] Bitget API error: %s", worker.name, exc)
            except Exception:
                log.exception("[%s] unexpected error", worker.name)
        if args.once:
            break
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
