"""Platform wrapper around the legacy Binance spot client.

The legacy `binance_client.BinanceClient` (repo root) already handles signing,
precision, min-notional and error normalization — this wrapper adapts it to the
platform's needs (dry-run simulation, fill math) without rewriting any of it.
All methods are synchronous (the legacy client uses `requests`); async callers
run them via `asyncio.to_thread`.
"""
import uuid

from binance_client import BinanceClient as _LegacyClient
from exchange_errors import ExchangeAPIError


class BinanceClientError(Exception):
    """Normalized failure from any Binance interaction."""

    def __init__(self, message: str, code: str = "BINANCE"):
        super().__init__(message)
        self.code = code


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str):
        self._client = _LegacyClient(api_key=api_key, api_secret=api_secret)

    def get_price(self, symbol: str) -> float:
        """Current spot price for a symbol like 'BTCUSDT'."""
        try:
            return self._client.get_ticker_price(symbol)
        except ExchangeAPIError as exc:
            raise BinanceClientError(str(exc), getattr(exc, "code", "BINANCE")) from exc

    def get_account_balance(self, coin: str) -> float:
        """Available (free) spot balance of `coin` (authenticated)."""
        try:
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
