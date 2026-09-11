"""Bitget public market data with a one-hour Redis cache: spot symbols (pair
exists, online, minimum order, precisions, taker fee) and coin chains
(withdrawable, fee, minimum, precision). Used to validate every purchase at
creation and again at execution, and to price the withdrawal minimum check.

Both async (routes) and sync (the executor thread) accessors read and write
the same cache keys. Field names verified against the live endpoints on
2026-09-11 (see docs/API.md, "Bitget market data").
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import redis as redis_sync

from api.config import settings

logger = logging.getLogger("autobuy.bitget_market")

CACHE_TTL_S = 3600
_SYMBOL_KEY = "bitget:market:symbol:{symbol}"
_CHAINS_KEY = "bitget:market:chains:{coin}"


class MarketDataUnavailable(RuntimeError):
    """Bitget could not be reached and nothing is cached."""


def _client():
    from bitget_client import BitgetClient
    return BitgetClient()   # public endpoints: no credentials


# ------------------------------------------------------------------ normalisation

def _num(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def normalize_symbol(raw: dict) -> dict:
    return {
        "symbol": raw.get("symbol"), "base": raw.get("baseCoin"), "quote": raw.get("quoteCoin"),
        "status": str(raw.get("status", "")).lower(), "online": str(raw.get("status", "")).lower() == "online",
        "min_trade_usdt": _num(raw.get("minTradeUSDT"), 0.0),
        "min_trade_amount": _num(raw.get("minTradeAmount"), 0.0),
        "max_market_order_value": _num(raw.get("maxMarketOrderValue")),
        "price_precision": int(_num(raw.get("pricePrecision"), 8)),
        "quantity_precision": int(_num(raw.get("quantityPrecision"), 8)),
        "quote_precision": int(_num(raw.get("quotePrecision"), 8)),
        "taker_fee_rate": _num(raw.get("takerFeeRate")), "maker_fee_rate": _num(raw.get("makerFeeRate")),
    }


def normalize_chain(raw: dict) -> dict:
    return {
        "chain": raw.get("chain"),
        "withdrawable": str(raw.get("withdrawable", "")).lower() == "true",
        "rechargeable": str(raw.get("rechargeable", "")).lower() == "true",
        "withdraw_fee": _num(raw.get("withdrawFee"), 0.0),
        "extra_withdraw_fee_pct": _num(raw.get("extraWithdrawFee"), 0.0),   # 0.1 means 10%
        "min_withdraw_amount": _num(raw.get("minWithdrawAmount"), 0.0),
        "withdraw_min_scale": int(_num(raw.get("withdrawMinScale"), 8)),
        "withdraw_confirm": int(_num(raw.get("withdrawConfirm"), 0)),
        "need_tag": str(raw.get("needTag", "")).lower() == "true",
        "contract_address": raw.get("contractAddress") or None,
        "browser_url": raw.get("browserUrl") or None,
        "congestion": raw.get("congestion"),
    }


# ------------------------------------------------------------------ fetchers (patched in tests)

def fetch_symbol(symbol: str) -> Optional[dict]:
    """Raw symbol record from Bitget, or None when the pair does not exist."""
    from exchange_errors import ExchangeAPIError
    try:
        return _client().get_symbol_info(symbol)
    except ExchangeAPIError as exc:
        if str(exc.code) == "SYMBOL":
            return None
        raise


def fetch_chains(coin: str) -> list:
    from exchange_errors import ExchangeAPIError
    try:
        return _client().get_coin_info(coin)
    except ExchangeAPIError as exc:
        if str(exc.code) == "SYMBOL":
            return []
        raise


# ------------------------------------------------------------------ cache plumbing

def _sync_redis():
    return redis_sync.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


def _cached_sync(key: str, producer):
    try:
        r = _sync_redis()
        try:
            raw = r.get(key)
            if raw is not None:
                return json.loads(raw)
        finally:
            r.close()
    except Exception:
        logger.warning("bitget market cache read failed for %s", key)
    try:
        value = producer()
    except Exception as exc:
        raise MarketDataUnavailable(f"Bitget market data unavailable for {key}: {exc}") from exc
    try:
        r = _sync_redis()
        try:
            r.setex(key, CACHE_TTL_S, json.dumps(value))
        finally:
            r.close()
    except Exception:
        logger.warning("bitget market cache write failed for %s", key)
    return value


async def _cached(key: str, producer):
    from api.services.redis_client import get_redis
    try:
        raw = await get_redis().get(key)
        if raw is not None:
            return json.loads(raw)
    except Exception:
        logger.warning("bitget market cache read failed for %s", key)
    try:
        value = await asyncio.to_thread(producer)
    except Exception as exc:
        raise MarketDataUnavailable(f"Bitget market data unavailable for {key}: {exc}") from exc
    try:
        await get_redis().setex(key, CACHE_TTL_S, json.dumps(value))
    except Exception:
        logger.warning("bitget market cache write failed for %s", key)
    return value


def _symbol_value(coin: str):
    raw = fetch_symbol(f"{coin.upper()}USDT")
    return normalize_symbol(raw) if raw else {"symbol": f"{coin.upper()}USDT", "missing": True}


def _chains_value(coin: str):
    return [normalize_chain(c) for c in fetch_chains(coin.upper())]


# ------------------------------------------------------------------ public API

async def get_symbol(coin: str) -> Optional[dict]:
    v = await _cached(_SYMBOL_KEY.format(symbol=f"{coin.upper()}USDT"), lambda: _symbol_value(coin))
    return None if v.get("missing") else v


def get_symbol_sync(coin: str) -> Optional[dict]:
    v = _cached_sync(_SYMBOL_KEY.format(symbol=f"{coin.upper()}USDT"), lambda: _symbol_value(coin))
    return None if v.get("missing") else v


async def get_chains(coin: str) -> list:
    return await _cached(_CHAINS_KEY.format(coin=coin.upper()), lambda: _chains_value(coin))


def get_chains_sync(coin: str) -> list:
    return _cached_sync(_CHAINS_KEY.format(coin=coin.upper()), lambda: _chains_value(coin))


def find_chain(chains: list, chain: str) -> Optional[dict]:
    want = (chain or "").strip().lower()
    return next((c for c in chains if (c.get("chain") or "").lower() == want), None)


# ------------------------------------------------------------------ validation

class PurchaseValidationError(ValueError):
    def __init__(self, error: str, details: dict):
        super().__init__(details.get("message", error))
        self.error = error
        self.details = details


def check_symbol(coin: str, amount_usd: float, symbol: Optional[dict]) -> None:
    """Pair exists, is online, and the order is above Bitget's minimum notional."""
    if symbol is None:
        raise PurchaseValidationError("pair_not_available", {
            "coin": coin, "pair": f"{coin}USDT",
            "message": f"{coin}/USDT is not a tradable spot pair on Bitget"})
    if not symbol.get("online"):
        raise PurchaseValidationError("pair_not_trading", {
            "coin": coin, "pair": symbol["symbol"], "status": symbol.get("status"),
            "message": f"{symbol['symbol']} is not trading on Bitget right now (status {symbol.get('status')})"})
    min_usdt = symbol.get("min_trade_usdt") or 0.0
    if amount_usd < min_usdt:
        raise PurchaseValidationError("below_minimum_order", {
            "coin": coin, "amount_usd": amount_usd, "minimum_usd": min_usdt,
            "message": f"Bitget requires at least {min_usdt:g} USDT per {symbol['symbol']} order"})
    max_value = symbol.get("max_market_order_value")
    if max_value and amount_usd > max_value:
        raise PurchaseValidationError("above_maximum_order", {
            "coin": coin, "amount_usd": amount_usd, "maximum_usd": max_value,
            "message": f"Bitget caps market orders on {symbol['symbol']} at {max_value:g} USDT"})


def estimate_received(amount_usd: float, price: float, symbol: dict, chain: dict) -> dict:
    """Coins the wallet would receive for a buy of amount_usd at `price`:
    quantity after the taker fee, floored to the chain's withdrawal precision,
    minus the chain's withdrawal fee (and percentage fee)."""
    fee_rate = symbol.get("taker_fee_rate") or 0.0
    bought = amount_usd / price
    after_trade_fee = bought * (1 - fee_rate)
    scale = chain.get("withdraw_min_scale", 8)
    factor = 10 ** scale
    size = int(after_trade_fee * factor) / factor
    withdraw_fee = (chain.get("withdraw_fee") or 0.0) + size * (chain.get("extra_withdraw_fee_pct") or 0.0)
    return {"bought": bought, "withdraw_size": size, "withdraw_fee": withdraw_fee,
            "received": max(size - withdraw_fee, 0.0), "min_withdraw_amount": chain.get("min_withdraw_amount") or 0.0}


def check_withdrawal(coin: str, amount_usd: float, price: float, symbol: dict, chain: dict) -> dict:
    """The chain is withdrawable and the amount after fees clears Bitget's minimum."""
    if not chain.get("withdrawable"):
        raise PurchaseValidationError("chain_not_withdrawable", {
            "coin": coin, "chain": chain.get("chain"),
            "message": f"Bitget does not allow {coin} withdrawals on {chain.get('chain')} right now"})
    est = estimate_received(amount_usd, price, symbol, chain)
    if est["withdraw_size"] < est["min_withdraw_amount"] + est["withdraw_fee"] or est["received"] <= 0:
        needed_coins = est["min_withdraw_amount"] + est["withdraw_fee"]
        min_usd = needed_coins * price / max(1 - (symbol.get("taker_fee_rate") or 0.0), 1e-9)
        raise PurchaseValidationError("below_minimum_withdrawal", {
            "coin": coin, "chain": chain.get("chain"), "amount_usd": amount_usd,
            "estimated_coins": round(est["withdraw_size"], 8),
            "min_withdraw_amount": est["min_withdraw_amount"], "withdraw_fee": round(est["withdraw_fee"], 8),
            "minimum_usd": round(min_usd * 1.01, 2),
            "message": (f"{amount_usd:g} USD buys about {est['withdraw_size']:.8f} {coin}, below Bitget's minimum "
                        f"withdrawal on {chain.get('chain')} ({est['min_withdraw_amount']:g} {coin} + "
                        f"{est['withdraw_fee']:.8f} fee). Use at least about {min_usd * 1.01:.2f} USD."),
        })
    return est


def live_ask(coin: str) -> float:
    """Current best ask (no cache) for the minimum-withdrawal estimate."""
    t = _client().get_ticker(f"{coin.upper()}USDT")
    price = t.get("ask") or t.get("last")
    if not price or price <= 0:
        raise MarketDataUnavailable(f"no live price for {coin}")
    return price
