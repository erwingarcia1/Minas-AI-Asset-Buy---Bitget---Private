"""Venue-agnostic exchange client for the platform.

The root-level exchange clients (`bitget_client`, and `binance_client` until
audit item C3 retires it) handle signing, precision, min-notional and error
normalization; this wrapper adapts them to the platform's needs (dry-run
simulation, fill math, permission interpretation) without rewriting any of it.
`exchange` picks which one signs the calls. All methods are synchronous (the
clients use `requests`); async callers run them via `asyncio.to_thread`.
Bybit and OKX support was removed on 2026-09-22 (audit Batch C1): Bitget is
the only venue keys can be created for.

Renamed from api/services/binance_client.py on 2026-09-22; that path is a
one-release alias re-exporting everything here.
"""
import logging
import uuid

from binance_client import BinanceClient as _LegacyBinance
from bitget_client import BitgetClient as _LegacyBitget
from exchange_errors import ExchangeAPIError

from api.services.metrics import time_exchange

logger = logging.getLogger("autobuy.exchange_client")

LEGACY_CLIENTS = {
    "binance": _LegacyBinance,
    "bitget": _LegacyBitget,
}

# kept for existing imports/monkeypatches
_LegacyClient = _LegacyBinance


# Bitget reports a key's permissions in /api/v2/spot/account/info "authorities".
# Bitget reports a key's permissions as short codes in `authorities`
# (GET /api/v2/spot/account/info). Confirmed from Bitget's "Edit API key"
# screen on 2026-09-11:
#   stow = Spot -> Trade            (REQUIRED for purchases; a Read-write key)
#   stor = Spot -> Read             (a READ-ONLY key with Spot ticked: it authenticates and
#                                    reads balances, but every order fails with 40014.
#                                    Seen live on 2026-10-06; rejected since v0.7.4.1)
#   smow = Margin -> Trade          (spot margin; not needed)
#   taxw = Taxation -> Search       (read trading history; not needed)
#   p2pw = P2P -> Search            (read merchant info; not needed)
#   wtow = Wallet -> Transfer       (internal transfers between the user's own
#                                    Bitget accounts; NOT an external withdrawal)
# plus the documented words readonly / read / trade / withdraw / transfer.
# The code Bitget uses for external Withdraw is still unconfirmed, so ANY other
# code is treated as "withdrawal possible" until it has been confirmed.
BITGET_TRADE_TOKENS = {"trade", "stow"}
BITGET_READ_ONLY_TOKENS = {"stor"}      # Spot -> Read: the key is Read-only, orders are refused (40014)
BITGET_WITHDRAW_TOKENS = {"withdraw"}
# recognised, harmless for withdrawals, but more than AutoBuy needs
BITGET_EXTRA_TOKENS = {"smow", "taxw", "p2pw", "wtow", "transfer"}
BITGET_EXTRA_LABELS = {"smow": "Margin trade", "taxw": "Taxation search", "p2pw": "P2P search",
                       "wtow": "Wallet transfer (internal)", "transfer": "Transfer (internal)"}
BITGET_KNOWN_TOKENS = BITGET_TRADE_TOKENS | BITGET_READ_ONLY_TOKENS | BITGET_WITHDRAW_TOKENS | BITGET_EXTRA_TOKENS | {"readonly", "read"}


# Unified Trading Account (v3) key permissions, from /api/v3/account/info (docs read 2026-10-08):
#   permType   read-only | read-and-write
#   permissions uta_trade (trade), uta_mgt (account management/read), withdraw, copy_futures_*
UTA_TRADE_TOKENS = {"uta_trade"}
UTA_WITHDRAW_TOKENS = {"withdraw"}
UTA_KNOWN_TOKENS = UTA_TRADE_TOKENS | UTA_WITHDRAW_TOKENS | {"uta_mgt", "copy_futures_position", "copy_futures_order"}


def interpret_unified_permissions(perms: list, perm_type) -> dict:
    """{can_trade, can_withdraw, permissions_known, unconfirmed, extra, read_only} for a Unified key."""
    tokens = {p.strip().lower() for p in perms if p}
    read_only = str(perm_type or "").lower() in ("read-only", "readonly", "read_only")
    trade = bool(tokens & UTA_TRADE_TOKENS) and not read_only
    withdraw = bool(tokens & UTA_WITHDRAW_TOKENS)
    unconfirmed = sorted(tokens - UTA_KNOWN_TOKENS)
    return {"can_trade": trade, "can_withdraw": withdraw, "permissions_known": bool(tokens) or bool(perm_type),
            "unconfirmed": unconfirmed, "extra": sorted(tokens & {"copy_futures_position", "copy_futures_order"}),
            "read_only": read_only,
            # Management -> Read (uta_mgt) is REQUIRED on a Unified key: the balance reads (/api/v3/account/assets) need it
            "management_read": "uta_mgt" in tokens}


def interpret_bitget_authorities(auths: list) -> dict:
    """{can_trade, can_withdraw, permissions_known, unconfirmed, extra} from
    Bitget's authorities list.

    can_trade is True with stow/trade, else False (spot trading is required).
    can_withdraw is True with the documented "withdraw", False when every code
    is recognised and none grants withdrawal, and None ("possible" — treat as
    enabled) when the list is empty or carries codes not yet confirmed.
    ``unconfirmed`` lists those codes; ``extra`` lists recognised codes the
    platform does not need (margin, transfer, taxation/P2P search).
    """
    tokens = {a.strip().lower() for a in auths if a}
    unconfirmed = sorted(tokens - BITGET_KNOWN_TOKENS)
    extra = sorted(tokens & BITGET_EXTRA_TOKENS)
    if not tokens:
        return {"can_trade": None, "can_withdraw": None, "permissions_known": False, "unconfirmed": [], "extra": [], "read_only": False}
    trade = bool(tokens & BITGET_TRADE_TOKENS)
    withdraw = bool(tokens & BITGET_WITHDRAW_TOKENS)
    read_only = bool(tokens & BITGET_READ_ONLY_TOKENS) and not trade   # Spot -> Read without Spot -> Trade
    fully_understood = not unconfirmed
    return {
        "read_only": read_only,
        "can_trade": False if read_only else (True if trade else (False if fully_understood else None)),
        "can_withdraw": True if withdraw else (False if fully_understood else None),
        "permissions_known": trade or withdraw or fully_understood,
        "unconfirmed": unconfirmed,
        "extra": extra,
    }


class ExchangeClientError(Exception):
    """Normalized failure from any exchange interaction."""

    def __init__(self, message: str, code: str = "BINANCE"):
        super().__init__(message)
        self.code = code


class ExchangeClient:
    """Venue-agnostic wrapper: the Bitget and (until C3) Binance transports
    share the same normalized interface, so `exchange` just picks which one
    signs the calls and which symbol format applies."""

    def __init__(self, api_key: str, api_secret: str, passphrase: str = "",
                 exchange: str = "binance", account_mode: str = "unknown"):
        self.exchange = exchange
        self.account_mode = account_mode if account_mode in ("classic", "unified", "unknown") else "unknown"
        if exchange == "binance":
            self._client = _LegacyClient(api_key=api_key, api_secret=api_secret)
        elif exchange == "bitget" and self.account_mode == "unified":   # v0.8.0: Unified Trading Account, v3
            from bitget_client import BitgetUnifiedClient
            self._client = BitgetUnifiedClient(api_key=api_key, api_secret=api_secret, passphrase=passphrase)
        else:   # classic and unknown: the Classic (v2) client, unchanged
            legacy = LEGACY_CLIENTS[exchange]
            self._client = legacy(api_key=api_key, api_secret=api_secret,
                                  passphrase=passphrase)

    def spot_symbol(self, base: str, quote: str = "USDT") -> str:
        return type(self._client).spot_symbol(base, quote)

    def get_price(self, symbol: str) -> float:
        """Current spot price for a symbol like 'BTCUSDT'."""
        try:
            with time_exchange("get_price"):
                return self._client.get_ticker_price(symbol)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    def get_account_info(self) -> dict:
        """Account trade/withdraw permissions (authenticated). Used to verify keys.

        Returns {can_trade, can_withdraw, permissions, permissions_known}.
        Bitget reports the key's permissions as authority codes (see
        interpret_bitget_authorities); an empty or unknown list yields
        ``permissions_known`` False with can_trade/can_withdraw None.
        """
        try:
            if self.exchange == "binance":
                data = self._client._request(
                    "GET", "/api/v3/account", params={"omitZeroBalances": "true"}, auth=True)
                return {
                    "can_trade": bool(data.get("canTrade")),
                    "can_withdraw": bool(data.get("canWithdraw")),
                    "permissions": data.get("permissions") or [],
                    "permissions_known": True,
                }
            info = self._client.get_account_info()
            auths = [a.lower() for a in info.get("authorities") or []]
            if self.account_mode == "unified":
                return {**interpret_unified_permissions(auths, info.get("perm_type")), "permissions": auths}
            return {**interpret_bitget_authorities(auths), "permissions": auths}
        except ExchangeAPIError as exc:
            raise ExchangeClientError(exc.message if hasattr(exc, "message") else str(exc),
                                     str(getattr(exc, "code", "BINANCE"))) from exc

    def get_account_balance(self, coin: str) -> float:
        """Available (free) spot balance of `coin` (authenticated)."""
        try:
            with time_exchange("get_balance"):
                return self._client.get_spot_available(coin)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    def get_funding_available(self, coin: str) -> float:
        """Available `coin` in the venue's Funding account (Unified Bitget only;
        read-only, used for the #91 top-up hint). AttributeError on venues
        without a funding read — callers treat that as "no hint"."""
        try:
            with time_exchange("get_funding"):
                return self._client.get_funding_available(coin)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    def execute_order(self, symbol: str, usdt_amount: float, dry_run: bool = True,
                      client_oid: str | None = None) -> dict:
        """Market-buy `usdt_amount` USDT worth of `symbol`.

        Returns {order_id, amount_crypto, exchange_rate, cost_usdt, dry_run}.
        Dry runs price the order but place nothing.
        """
        try:
            if dry_run:
                # Price a dry run at the venue's live best ask (what a market buy
                # would pay), falling back to the last trade only when the
                # venue's ticker has no ask. No rounding, no fixed fallback: a
                # missing or non-positive price fails the run.
                if self.exchange == "bitget":
                    ticker = self._client.get_ticker(symbol)
                    price = ticker.get("ask") or ticker.get("last")
                    source = "ask" if ticker.get("ask") else "last"
                else:   # Binance transport (switched off in production; retired in C3): last trade only
                    ticker = {"last": self._client.get_ticker_price(symbol)}
                    price, source = ticker["last"], "last"
                if not price or price <= 0:
                    raise ExchangeClientError(f"live price for {symbol} unavailable from {self.exchange} "
                                             f"(ticker: {ticker}); dry run aborted", "PRICE_UNAVAILABLE")
                logger.info("dry-run priced %s at %s %.8g (last=%s ask=%s bid=%s ts=%s)", symbol, source, price,
                            ticker.get("last"), ticker.get("ask"), ticker.get("bid"), ticker.get("ts"))
                return {
                    "order_id": f"DRY-{uuid.uuid4().hex[:12]}",
                    "amount_crypto": usdt_amount / price,
                    "exchange_rate": price,
                    "cost_usdt": usdt_amount,
                    "dry_run": True,
                    "price_source": f"{self.exchange}:{source}",
                }
            price = self._client.get_ticker_price(symbol)

            info = self._client.get_symbol_info(symbol)
            if info["status"] != "online":
                raise ExchangeClientError(f"{symbol} is not trading (status={info['status']})", "SYMBOL")
            if usdt_amount < float(info["minTradeUSDT"]):
                raise ExchangeClientError(
                    f"{usdt_amount} USDT is below the {info['minTradeUSDT']} USDT minimum for {symbol}",
                    "MIN_NOTIONAL",
                )

            with time_exchange("place_order"):
                order = self._client.place_market_buy(symbol, usdt_amount, client_oid=client_oid)
            executed_base = float(order.get("executedQty") or 0)
            executed_quote = float(order.get("cummulativeQuoteQty") or 0)
            if executed_base <= 0:
                raise ExchangeClientError(f"order {order.get('orderId')} reported no fill", "NO_FILL")
            return {
                "order_id": str(order["orderId"]),
                "amount_crypto": executed_base,
                "exchange_rate": executed_quote / executed_base,
                "cost_usdt": executed_quote,
                "dry_run": False,
            }
        except ExchangeClientError:
            raise
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc


    def withdraw(self, coin: str, address: str, amount: float, network: str | None = None,
                 client_oid: str | None = None, dry_run: bool = True, travel_rule: dict | None = None,
                 tag: str | None = None) -> dict:
        """Withdraw `amount` of `coin` to `address` (`tag` = destination tag / memo on
        chains that use one; only Bitget receives it).

        Returns {withdrawal_id, amount, dry_run}. Dry runs validate inputs and
        submit nothing.
        """
        if amount <= 0:
            raise ExchangeClientError("withdrawal amount must be positive", "AMOUNT")
        if dry_run:
            return {"withdrawal_id": f"DRY-WD-{uuid.uuid4().hex[:12]}", "amount": amount, "dry_run": True}
        try:
            with time_exchange("withdraw"):
                kwargs = {"travel_rule": travel_rule} if (travel_rule and self.exchange == "bitget") else {}
                if tag and self.exchange == "bitget":
                    kwargs["tag"] = tag
                wid = self._client.withdraw(coin, address, amount, network=network, client_oid=client_oid, **kwargs)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc
        if not wid:
            raise ExchangeClientError("exchange returned no withdrawal id", "NO_WITHDRAWAL_ID")
        return {"withdrawal_id": wid, "amount": amount, "dry_run": False}


    def get_available(self, coin: str) -> float:
        """Free spot balance of `coin` on the account (checked before a live withdrawal)."""
        try:
            with time_exchange("available"):
                return float(self._client.get_spot_available(coin))
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    # Exchange withdrawal statuses -> (done, ok). Anything else is still in flight.
    WITHDRAWAL_TERMINAL = {"success": (True, True), "wallet_fail": (True, False), "cancel": (True, False),
                           "reject": (True, False), "fail": (True, False), "failed": (True, False)}

    def withdrawal_status(self, coin: str, client_oid: str | None, withdrawal_id: str | None,
                          since_ms: int, until_ms: int) -> dict:
        """The exchange's record of one withdrawal:
        {found, raw_status, tx_hash, fee, done, ok}. `ok` is None while in flight."""
        try:
            with time_exchange("withdrawal_records"):
                rows = self._client.get_withdrawal_records(coin, since_ms, until_ms, client_oid=client_oid,
                                                           order_id=withdrawal_id)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc
        row = next((r for r in rows or [] if (withdrawal_id and str(r.get("orderId")) == str(withdrawal_id))
                    or (client_oid and r.get("clientOid") == client_oid)), None)
        if row is None:
            row = rows[0] if len(rows or []) == 1 else None
        if row is None:
            return {"found": False, "raw_status": None, "tx_hash": None, "fee": None, "done": False, "ok": None}
        raw = str(row.get("status") or "").lower()
        done, ok = self.WITHDRAWAL_TERMINAL.get(raw, (False, None))
        fee = row.get("fee")
        return {"found": True, "raw_status": raw, "tx_hash": row.get("tradeId") or None,
                "fee": float(fee) if fee not in (None, "") else None, "done": done, "ok": ok}

    # ------------------------------------------------------------- idempotent buy flow

    def place_buy(self, symbol: str, usdt_amount: float, client_oid: str, dry_run: bool = True) -> dict:
        """Send a market buy tagged with our ``client_oid`` and return ``{"order_id"}``
        without reading the fill (that is :meth:`fetch_fill`). Dry runs send nothing."""
        if dry_run:
            return {"order_id": f"DRY-{client_oid}", "dry_run": True}
        try:
            info = self._client.get_symbol_info(symbol)
            if str(info.get("status", "online")).lower() not in ("online", "trading", "1"):
                raise ExchangeClientError(f"{symbol} is not trading (status={info.get('status')})", "SYMBOL")
            min_usdt = info.get("minTradeUSDT")
            if min_usdt not in (None, "") and usdt_amount < float(min_usdt):
                raise ExchangeClientError(
                    f"{usdt_amount} USDT is below the {min_usdt} USDT minimum for {symbol}", "MIN_NOTIONAL")
            with time_exchange("place_order"):
                order = self._client.place_market_buy(symbol, usdt_amount, client_oid=client_oid)
        except ExchangeClientError:
            raise
        except ExchangeAPIError as exc:
            raise ExchangeClientError(exc.message, str(getattr(exc, "code", "EXCHANGE"))) from exc
        order_id = str((order or {}).get("orderId") or (order or {}).get("order_id") or "")
        if not order_id:
            raise ExchangeClientError("exchange returned no order id", "NO_ORDER_ID")
        return {"order_id": order_id, "dry_run": False}

    def fetch_fill(self, symbol: str, usdt_amount: float, order_id: str, client_oid: str,
                   dry_run: bool = True) -> dict:
        """Read the fill of an order: {order_id, filled, raw_status, amount_crypto,
        exchange_rate, cost_usdt, fee, fee_currency}. Dry runs are priced at the
        live best ask (see execute_order) with the venue's taker fee simulated in
        the base coin."""
        if dry_run:
            sim = self.execute_order(symbol, usdt_amount, dry_run=True, client_oid=client_oid)
            fee = fee_currency = None
            try:
                rate = self._client.get_symbol_info(symbol).get("takerFeeRate")
                if rate not in (None, ""):
                    fee, fee_currency = sim["amount_crypto"] * float(rate), symbol[: -len("USDT")] if symbol.endswith("USDT") else None
            except Exception:  # fee simulation is best effort; the fill is not
                pass
            return {"order_id": order_id, "filled": True, "raw_status": "filled (dry run)",
                    "amount_crypto": sim["amount_crypto"], "exchange_rate": sim["exchange_rate"],
                    "cost_usdt": sim["cost_usdt"], "fee": fee, "fee_currency": fee_currency,
                    "price_source": sim.get("price_source")}
        try:
            with time_exchange("order_info"):
                order = self._client.get_order_info(symbol, order_id=order_id) if order_id else \
                    self._client.get_order_info(symbol, client_oid=client_oid)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(exc.message, str(getattr(exc, "code", "EXCHANGE"))) from exc
        if order is None:
            raise ExchangeClientError(f"order {order_id or client_oid} not found on {self.exchange}", "ORDER_NOT_FOUND")
        return self._normalize_order(order, order_id)

    def lookup_order(self, symbol: str, client_oid: str) -> dict:
        """State of an order we may or may not have sent, by our client id:
        {"state": filled | open | cancelled | not_found, "fill": {...} | None}."""
        try:
            order = self._client.get_order_info(symbol, client_oid=client_oid)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(exc.message, str(getattr(exc, "code", "EXCHANGE"))) from exc
        if order is None:
            return {"state": "not_found", "fill": None}
        fill = self._normalize_order(order, order.get("orderId") or "")
        status = fill["raw_status"]
        if fill["filled"]:
            return {"state": "filled", "fill": fill}
        if status in ("cancelled", "canceled", "rejected", "expired"):
            return {"state": "cancelled", "fill": fill}
        return {"state": "open", "fill": fill}

    @staticmethod
    def _normalize_order(order: dict, order_id: str) -> dict:
        status = str(order.get("status", "")).lower()
        base = float(order.get("baseVolume") or 0)
        quote = float(order.get("quoteVolume") or 0)
        avg = order.get("priceAvg")
        avg = float(avg) if avg not in (None, "") else (quote / base if base else None)
        filled = status in ("filled", "full_fill", "closed") or (status == "" and base > 0)
        return {"order_id": str(order.get("orderId") or order_id), "filled": filled, "raw_status": status or "unknown",
                "amount_crypto": base, "exchange_rate": avg, "cost_usdt": quote,
                "fee": order.get("fee"), "fee_currency": order.get("feeCoin")}

    # ------------------------------------------------------------- withdrawal data

    def get_coin_chains(self, coin: str) -> list:
        """Withdrawal chains for a coin (/api/v2/spot/public/coins)."""
        try:
            return self._client.get_coin_info(coin)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(exc.message, str(getattr(exc, "code", "EXCHANGE"))) from exc

    def get_symbol(self, symbol: str) -> dict:
        try:
            return self._client.get_symbol_info(symbol)
        except ExchangeAPIError as exc:
            raise ExchangeClientError(exc.message, str(getattr(exc, "code", "EXCHANGE"))) from exc


# Former names, kept for one release (2026-09-22 rename).
BinanceClient = ExchangeClient
BinanceClientError = ExchangeClientError
