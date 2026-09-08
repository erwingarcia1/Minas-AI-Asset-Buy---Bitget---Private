"""Minimal signed REST client for the Binance spot API.

Implements the SIGNED-endpoint scheme documented at
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information :
signature = hex( HMAC-SHA256( secret, totalParams ) )
where totalParams is the urlencoded query string (GET) or the
x-www-form-urlencoded request body (POST), including timestamp (epoch ms)
and recvWindow. The signature is appended as &signature= and the API key
travels in the X-MBX-APIKEY header. Binance has no passphrase.
"""

import hashlib
import hmac
import logging
import re
import time
from urllib.parse import urlencode

import requests

from exchange_errors import ExchangeAPIError

log = logging.getLogger("binance")

BASE_URL = "https://api.binance.com"

# newClientOrderId must match ^[\.A-Z\:/a-z0-9_-]{1,36}$
_CLIENT_OID_BAD_CHARS = re.compile(r"[^\.A-Z:/a-z0-9_\-]")
_CLIENT_OID_MAX_LEN = 36

# Binance order states -> the bot's normalized lowercase states.
_ORDER_STATUS_MAP = {
    "NEW": "live",
    "PENDING_NEW": "live",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "PENDING_CANCEL": "cancelled",
    "EXPIRED": "cancelled",
    "EXPIRED_IN_MATCH": "cancelled",
    "REJECTED": "rejected",
}

_INVALID_SYMBOL = "-1121"   # {"code": -1121, "msg": "Invalid symbol."}
_ORDER_NOT_FOUND = "-2013"  # {"code": -2013, "msg": "Order does not exist."}


class BinanceAPIError(ExchangeAPIError):
    """Raised when Binance returns an HTTP >= 400 body or the request fails."""

    exchange = "Binance"


class BinanceClient:
    def __init__(self, api_key="", api_secret="", passphrase="", base_url=BASE_URL, timeout=15):
        # `passphrase` is accepted only so every exchange client constructs
        # identically; Binance does not use one.
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------ auth

    def _sign(self, total_params):
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            total_params.encode("utf-8"),
            hashlib.sha256,
        )
        return digest.hexdigest()

    def _request(self, method, path, params=None, auth=False):
        params = dict(params or {})
        headers = {}
        if auth:
            params["recvWindow"] = 5000
            params["timestamp"] = int(time.time() * 1000)
        encoded = urlencode(params)
        if auth:
            headers["X-MBX-APIKEY"] = self.api_key
            encoded = f"{encoded}&signature={self._sign(encoded)}"

        url = self.base_url + path
        try:
            if method.upper() == "POST":
                # Signed POST params go in the x-www-form-urlencoded body; the
                # signature was computed over that exact body string.
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                resp = self.session.request(method, url, data=encoded, headers=headers, timeout=self.timeout)
            else:
                if encoded:
                    url = f"{url}?{encoded}"
                resp = self.session.request(method, url, headers=headers, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise BinanceAPIError("NETWORK", f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError:
            raise BinanceAPIError("HTTP", f"non-JSON response ({resp.status_code}): {resp.text[:200]}", resp.status_code)

        if resp.status_code >= 400:
            code = payload.get("code", resp.status_code) if isinstance(payload, dict) else resp.status_code
            msg = payload.get("msg", resp.text[:200]) if isinstance(payload, dict) else resp.text[:200]
            raise BinanceAPIError(str(code), msg, resp.status_code)
        return payload

    # --------------------------------------------------------------- public

    @staticmethod
    def spot_symbol(base, quote):
        """Binance spot symbol format: ("XLM", "USDT") -> "XLMUSDT"."""
        return f"{base.upper()}{quote.upper()}"

    def get_server_time(self):
        """Binance server time in epoch milliseconds. No auth required."""
        return int(self._request("GET", "/api/v3/time")["serverTime"])

    def get_symbol_info(self, symbol):
        """Spot symbol metadata normalized to {status, minTradeUSDT, quotePrecision}."""
        try:
            data = self._request("GET", "/api/v3/exchangeInfo", params={"symbol": symbol})
        except BinanceAPIError as exc:
            if exc.code == _INVALID_SYMBOL:
                raise BinanceAPIError("SYMBOL", f"unknown spot symbol {symbol!r}", exc.http_status) from exc
            raise
        symbols = data.get("symbols") or []
        if not symbols:
            raise BinanceAPIError("SYMBOL", f"unknown spot symbol {symbol!r}")
        info = symbols[0]

        # Minimum quote value: prefer the NOTIONAL filter (only when it applies
        # to market orders), falling back to the legacy MIN_NOTIONAL filter.
        min_notional = "0"
        for f in info.get("filters", []):
            if f.get("filterType") == "NOTIONAL":
                if f.get("applyMinToMarket", True):
                    min_notional = f.get("minNotional", "0")
                break
            if f.get("filterType") == "MIN_NOTIONAL" and f.get("applyToMarket", True):
                min_notional = f.get("minNotional", "0")

        return {
            "status": "online" if info.get("status") == "TRADING" else info.get("status"),
            "minTradeUSDT": min_notional,
            "quotePrecision": int(info["quoteAssetPrecision"]),
        }

    def get_ticker_price(self, symbol):
        data = self._request("GET", "/api/v3/ticker/price", params={"symbol": symbol})
        return float(data["price"])

    # -------------------------------------------------------------- private

    def get_spot_available(self, coin):
        """Available (free) spot balance of `coin` for the account owning this API key."""
        data = self._request("GET", "/api/v3/account", params={"omitZeroBalances": "true"}, auth=True)
        for entry in data.get("balances") or []:
            if entry.get("asset", "").upper() == coin.upper():
                return float(entry.get("free", "0"))
        return 0.0

    @staticmethod
    def _sanitize_client_oid(client_oid):
        """Clamp a caller id to Binance's newClientOrderId charset and 36-char limit."""
        cleaned = _CLIENT_OID_BAD_CHARS.sub("", client_oid or "")
        return cleaned[:_CLIENT_OID_MAX_LEN] or None

    def place_market_buy(self, symbol, quote_amount, client_oid=None):
        """Market-buy `symbol` spending `quote_amount` of the quote coin (USDT).

        Uses quoteOrderQty, so the order is denominated in the quote currency.
        """
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": str(quote_amount),
        }
        oid = self._sanitize_client_oid(client_oid) if client_oid else None
        if oid:
            params["newClientOrderId"] = oid
        return self._request("POST", "/api/v3/order", params=params, auth=True)

    def withdraw(self, coin, address, amount, network=None, client_oid=None):
        """Submit a withdrawal via /sapi/v1/capital/withdraw/apply.

        `network` is Binance's network code (e.g. "BTC", "ETH", "SOL"); when
        omitted Binance uses the coin's default network. Returns the withdrawal
        id string. Requires an API key with withdrawal permission and a
        whitelisted address when the account enforces a whitelist.
        """
        params = {"coin": coin.upper(), "address": address, "amount": str(amount)}
        if network:
            params["network"] = network
        oid = self._sanitize_client_oid(client_oid) if client_oid else None
        if oid:
            params["withdrawOrderId"] = oid
        data = self._request("POST", "/sapi/v1/capital/withdraw/apply", params=params, auth=True)
        return str(data.get("id", ""))

    def get_order_info(self, symbol, order_id):
        """Order state normalized to {status, baseVolume, quoteVolume}, or None if unknown."""
        try:
            data = self._request("GET", "/api/v3/order", params={"symbol": symbol, "orderId": order_id}, auth=True)
        except BinanceAPIError as exc:
            if exc.code == _ORDER_NOT_FOUND:
                return None
            raise
        status = data.get("status", "")
        return {
            "status": _ORDER_STATUS_MAP.get(status, status),
            "baseVolume": data.get("executedQty"),
            # sic: Binance really spells it "cummulativeQuoteQty".
            "quoteVolume": data.get("cummulativeQuoteQty"),
        }
