"""Platform wrapper around the legacy Binance spot client.

The legacy `binance_client.BinanceClient` (repo root) already handles signing,
precision, min-notional and error normalization — this wrapper adapts it to the
platform's needs (dry-run simulation, fill math) without rewriting any of it.
All methods are synchronous (the legacy client uses `requests`); async callers
run them via `asyncio.to_thread`.
"""
import uuid

from binance_client import BinanceClient as _LegacyBinance
from bitget_client import BitgetClient as _LegacyBitget
from bybit_client import BybitClient as _LegacyBybit
from okx_client import OKXClient as _LegacyOKX
from exchange_errors import ExchangeAPIError

from api.services.metrics import time_binance

LEGACY_CLIENTS = {
    "binance": _LegacyBinance,
    "bitget": _LegacyBitget,
    "bybit": _LegacyBybit,
    "okx": _LegacyOKX,
}

# kept for existing imports/monkeypatches
_LegacyClient = _LegacyBinance


# Bitget reports a key's permissions in /api/v2/spot/account/info "authorities".
# The documented values are words ("readonly", "trade", "withdraw", "transfer");
# live accounts return undocumented short codes instead (a read + spot-trade
# key answered ["taxw", "p2pw", "wtow", "stow", "smow"] on 2026-09-11). Only
# the tokens below are interpreted — "stow" as spot trade, inferred from that
# key — everything else is recorded verbatim and left "unknown" rather than
# guessed, so an authenticated key is never wrongly rejected for lacking trade
# permission, and withdrawal is only flagged on the documented value.
BITGET_TRADE_TOKENS = {"trade", "stow"}
BITGET_WITHDRAW_TOKENS = {"withdraw"}
BITGET_TRANSFER_TOKENS = {"transfer"}


def interpret_bitget_authorities(auths: list) -> dict:
    """{can_trade, can_withdraw, permissions_known} from Bitget's authorities list.

    can_trade / can_withdraw are True when a known token is present, False when
    the list is non-empty and fully understood without it, and None (unknown)
    when the list is empty or contains codes this code does not interpret.
    """
    known_tokens = BITGET_TRADE_TOKENS | BITGET_WITHDRAW_TOKENS | BITGET_TRANSFER_TOKENS | {"readonly", "read"}
    tokens = {a.strip().lower() for a in auths if a}
    if not tokens:
        return {"can_trade": None, "can_withdraw": None, "permissions_known": False}
    trade = bool(tokens & BITGET_TRADE_TOKENS)
    withdraw = bool(tokens & BITGET_WITHDRAW_TOKENS)
    fully_understood = tokens <= known_tokens
    return {
        "can_trade": True if trade else (False if fully_understood else None),
        "can_withdraw": True if withdraw else (False if fully_understood else None),
        "permissions_known": trade or withdraw or fully_understood,
    }


class BinanceClientError(Exception):
    """Normalized failure from any Binance interaction."""

    def __init__(self, message: str, code: str = "BINANCE"):
        super().__init__(message)
        self.code = code


class BinanceClient:
    """Venue-agnostic wrapper (name kept for compatibility): all four legacy
    clients share the same normalized interface, so `exchange` just picks
    which one signs the calls and which symbol format applies."""

    def __init__(self, api_key: str, api_secret: str, passphrase: str = "",
                 exchange: str = "binance"):
        self.exchange = exchange
        if exchange == "binance":
            self._client = _LegacyClient(api_key=api_key, api_secret=api_secret)
        else:
            legacy = LEGACY_CLIENTS[exchange]
            self._client = legacy(api_key=api_key, api_secret=api_secret,
                                  passphrase=passphrase)

    def spot_symbol(self, base: str, quote: str = "USDT") -> str:
        return type(self._client).spot_symbol(base, quote)

    def get_price(self, symbol: str) -> float:
        """Current spot price for a symbol like 'BTCUSDT'."""
        try:
            with time_binance("get_price"):
                return self._client.get_ticker_price(symbol)
        except ExchangeAPIError as exc:
            raise BinanceClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    def get_account_info(self) -> dict:
        """Account trade/withdraw permissions (authenticated). Used to verify keys.

        Returns {can_trade, can_withdraw, permissions, permissions_known}.
        Binance and Bitget report the key's permissions; Bybit/OKX only prove
        the key works (a balance read), so ``permissions_known`` is False and
        can_trade/can_withdraw are None.
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
            if self.exchange == "bitget":
                info = self._client.get_account_info()
                auths = [a.lower() for a in info.get("authorities") or []]
                return {**interpret_bitget_authorities(auths), "permissions": auths}
            # other venues: an authenticated balance read proves the key works
            self._client.get_spot_available("USDT")
            return {"can_trade": None, "can_withdraw": None, "permissions": [], "permissions_known": False}
        except ExchangeAPIError as exc:
            raise BinanceClientError(exc.message if hasattr(exc, "message") else str(exc),
                                     str(getattr(exc, "code", "BINANCE"))) from exc

    def get_account_balance(self, coin: str) -> float:
        """Available (free) spot balance of `coin` (authenticated)."""
        try:
            with time_binance("get_balance"):
                return self._client.get_spot_available(coin)
        except ExchangeAPIError as exc:
            raise BinanceClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    def execute_order(self, symbol: str, usdt_amount: float, dry_run: bool = True,
                      client_oid: str | None = None) -> dict:
        """Market-buy `usdt_amount` USDT worth of `symbol`.

        Returns {order_id, amount_crypto, exchange_rate, cost_usdt, dry_run}.
        Dry runs price the order but place nothing.
        """
        try:
            price = self._client.get_ticker_price(symbol)
            if dry_run:
                return {
                    "order_id": f"DRY-{uuid.uuid4().hex[:12]}",
                    "amount_crypto": usdt_amount / price,
                    "exchange_rate": price,
                    "cost_usdt": usdt_amount,
                    "dry_run": True,
                }

            info = self._client.get_symbol_info(symbol)
            if info["status"] != "online":
                raise BinanceClientError(f"{symbol} is not trading (status={info['status']})", "SYMBOL")
            if usdt_amount < float(info["minTradeUSDT"]):
                raise BinanceClientError(
                    f"{usdt_amount} USDT is below the {info['minTradeUSDT']} USDT minimum for {symbol}",
                    "MIN_NOTIONAL",
                )

            with time_binance("place_order"):
                order = self._client.place_market_buy(symbol, usdt_amount, client_oid=client_oid)
            executed_base = float(order.get("executedQty") or 0)
            executed_quote = float(order.get("cummulativeQuoteQty") or 0)
            if executed_base <= 0:
                raise BinanceClientError(f"order {order.get('orderId')} reported no fill", "NO_FILL")
            return {
                "order_id": str(order["orderId"]),
                "amount_crypto": executed_base,
                "exchange_rate": executed_quote / executed_base,
                "cost_usdt": executed_quote,
                "dry_run": False,
            }
        except BinanceClientError:
            raise
        except ExchangeAPIError as exc:
            raise BinanceClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc


    def withdraw(self, coin: str, address: str, amount: float, network: str | None = None,
                 client_oid: str | None = None, dry_run: bool = True) -> dict:
        """Withdraw `amount` of `coin` to `address`.

        Returns {withdrawal_id, amount, dry_run}. Dry runs validate inputs and
        submit nothing. Only Binance withdrawals are implemented; other venues
        raise BinanceClientError(code="WITHDRAW_UNSUPPORTED") so the caller can
        record an honest failure instead of pretending the coins moved.
        """
        if amount <= 0:
            raise BinanceClientError("withdrawal amount must be positive", "AMOUNT")
        if dry_run:
            return {"withdrawal_id": f"DRY-WD-{uuid.uuid4().hex[:12]}", "amount": amount, "dry_run": True}
        if self.exchange != "binance":
            raise BinanceClientError(
                f"withdrawals are not implemented for {self.exchange}", "WITHDRAW_UNSUPPORTED")
        try:
            with time_binance("withdraw"):
                wid = self._client.withdraw(coin, address, amount, network=network, client_oid=client_oid)
        except ExchangeAPIError as exc:
            raise BinanceClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc
        if not wid:
            raise BinanceClientError("exchange returned no withdrawal id", "NO_WITHDRAWAL_ID")
        return {"withdrawal_id": wid, "amount": amount, "dry_run": False}


ExchangeClient = BinanceClient  # preferred name going forward
