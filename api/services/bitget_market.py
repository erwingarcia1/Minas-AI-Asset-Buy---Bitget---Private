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


UNAVAILABLE_MESSAGE = "Bitget market data is temporarily unavailable. Please try again in a minute."


class MarketDataUnavailable(RuntimeError):
    """Bitget could not be reached and nothing is cached. str(exc) is for logs;
    users get UNAVAILABLE_MESSAGE."""


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


FEE_MODES = ("exclusive", "inclusive")


def fee_mode() -> str:
    mode = (settings.bitget_withdraw_fee_mode or "exclusive").lower()
    return mode if mode in FEE_MODES else "exclusive"


def _floor(value: float, scale: int) -> float:
    factor = 10 ** int(scale)
    return int(value * factor + 1e-9) / factor


def withdrawal_plan(net_balance: float, chain: dict, mode: Optional[str] = None) -> dict:
    """How a withdrawal of `net_balance` coins (after the trading fee) is sized:

    exclusive (default): Bitget charges the fee ON TOP of size, so
        size = floor(net - fee), received = size, balance needed = size + fee
    inclusive: Bitget deducts the fee FROM size, so
        size = floor(net), received = size - fee, balance needed = size

    Sizes are floored to the chain's withdrawMinScale. `fee` includes the
    percentage extra fee when the chain has one. `sendable` is False when
    nothing positive would reach the wallet."""
    mode = mode or fee_mode()
    scale = chain.get("withdraw_min_scale", 8)
    flat = chain.get("withdraw_fee") or 0.0
    pct = chain.get("extra_withdraw_fee_pct") or 0.0
    if mode == "inclusive":
        size = _floor(max(net_balance, 0.0), scale)
        fee = flat + size * pct
        received = size - fee
        needed = size
    else:
        # the percentage fee applies to what is sent; solve size + flat + size*pct <= net
        size = _floor(max((net_balance - flat) / (1 + pct), 0.0), scale)
        fee = flat + size * pct
        received = size
        needed = size + fee
    return {"mode": mode, "size": size, "fee": fee, "received": max(received, 0.0), "balance_needed": needed,
            "sendable": size > 0 and received > 0}


def estimate_received(amount_usd: float, price: float, symbol: dict, chain: dict, mode: Optional[str] = None) -> dict:
    """What a buy of amount_usd at `price` turns into: coins after the taker
    fee, then the withdrawal plan for the configured fee mode."""
    fee_rate = symbol.get("taker_fee_rate") or 0.0
    bought = amount_usd / price
    after_trade_fee = bought * (1 - fee_rate)
    plan = withdrawal_plan(after_trade_fee, chain, mode)
    return {"bought": bought, "net": after_trade_fee, "withdraw_size": plan["size"], "withdraw_fee": plan["fee"],
            "received": plan["received"], "sendable": plan["sendable"], "mode": plan["mode"],
            "min_withdraw_amount": chain.get("min_withdraw_amount") or 0.0}


def meets_minimum(est: dict) -> bool:
    """Bitget's minimum applies to the amount actually sent."""
    return est["sendable"] and est["withdraw_size"] >= est["min_withdraw_amount"]


def minimum_purchase_usd(price: float, symbol: dict, chain: dict, mode: Optional[str] = None) -> float:
    """Smallest USD amount whose buy still clears minimum + fee, with a 1% margin."""
    mode = mode or fee_mode()
    min_amount = chain.get("min_withdraw_amount") or 0.0
    flat = chain.get("withdraw_fee") or 0.0
    pct = chain.get("extra_withdraw_fee_pct") or 0.0
    # exclusive: net >= min + flat + min*pct ; inclusive: net >= min (fee comes out of it), but received must be > 0
    needed_net = min_amount + flat + min_amount * pct if mode == "exclusive" else max(min_amount, flat / max(1 - pct, 1e-9) + 1e-8)
    fee_rate = symbol.get("taker_fee_rate") or 0.0
    return round(needed_net * price / max(1 - fee_rate, 1e-9) * 1.01, 2)


def check_withdrawal(coin: str, amount_usd: float, price: float, symbol: dict, chain: dict) -> dict:
    """The chain is withdrawable and the amount actually sent clears Bitget's minimum."""
    if not chain.get("withdrawable"):
        raise PurchaseValidationError("chain_not_withdrawable", {
            "coin": coin, "chain": chain.get("chain"),
            "message": f"Bitget does not allow {coin} withdrawals on {chain.get('chain')} right now"})
    est = estimate_received(amount_usd, price, symbol, chain)
    if not meets_minimum(est):
        min_usd = minimum_purchase_usd(price, symbol, chain)
        raise PurchaseValidationError("below_minimum_withdrawal", {
            "coin": coin, "chain": chain.get("chain"), "amount_usd": amount_usd,
            "estimated_coins": round(est["withdraw_size"], 8),
            "min_withdraw_amount": est["min_withdraw_amount"], "withdraw_fee": round(est["withdraw_fee"], 8),
            "fee_mode": est["mode"], "minimum_usd": min_usd,
            "message": (f"{amount_usd:g} USD buys about {est['net']:.8f} {coin}; after the {est['withdraw_fee']:.8f} "
                        f"{coin} withdrawal fee that is below Bitget's minimum withdrawal of "
                        f"{est['min_withdraw_amount']:g} {coin} on {chain.get('chain')}. Use at least about {min_usd:.2f} USD."),
        })
    return est


def live_ask(coin: str) -> float:
    """Current best ask (no cache) for the minimum-withdrawal estimate. Any
    failure to reach Bitget is MarketDataUnavailable (-> 503, nothing created),
    never a raw client error (-> 500)."""
    try:
        t = _client().get_ticker(f"{coin.upper()}USDT")
    except Exception as exc:
        raise MarketDataUnavailable(f"live price for {coin} unavailable: {exc}") from exc
    price = t.get("ask") or t.get("last")
    if not price or price <= 0:
        raise MarketDataUnavailable(f"no live price for {coin}")
    return price
