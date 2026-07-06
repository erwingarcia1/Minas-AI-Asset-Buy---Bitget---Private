"""Minimal Bybit v5 spot REST client for the auto-buy bot.

Only the endpoints the bot needs: server time, instrument info, ticker,
spot balance, quote-denominated market buy, and order status.

Signature scheme (docs: https://bybit-exchange.github.io/docs/v5/guide):
    X-BAPI-SIGN = hex(HMAC_SHA256(secret,
        timestamp + api_key + recv_window + payload))
where timestamp is epoch milliseconds, recv_window is "5000", and payload
is the url-encoded query string for GET or the raw JSON body for POST.
Success convention: retCode == 0 in {"retCode", "retMsg", "result"}.
"""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlencode

import requests

from exchange_errors import ExchangeAPIError


class BybitAPIError(ExchangeAPIError):
    exchange = "Bybit"


# Bybit order states -> normalized lowercase states used by the bot.
_STATUS_MAP = {
    "New": "live",
    "PartiallyFilled": "live",
    "Untriggered": "live",
    "Triggered": "live",
    "Filled": "filled",
    "Cancelled": "cancelled",
    "PartiallyFilledCanceled": "cancelled",
    "Deactivated": "cancelled",
    "Rejected": "rejected",
}

# orderLinkId: max 36 chars; numbers, letters, dashes and underscores only.
_OID_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")


class BybitClient:
    """Tiny typed wrapper around the Bybit v5 REST API (spot only)."""

    RECV_WINDOW = "5000"

    def __init__(self, api_key="", api_secret="", passphrase="",
                 base_url="https://api.bybit.com", timeout=15):
        # Bybit has no passphrase; accepted (and ignored) so every
        # exchange client constructs identically.
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Low-level request / signing
    # ------------------------------------------------------------------
    def _request(self, method, path, params=None, body=None, signed=False):
        query = urlencode(params) if params else ""
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = self.base_url + path + ("?" + query if query else "")

        headers = {"Content-Type": "application/json"}
        if signed:
            ts = str(int(time.time() * 1000))
            payload = body_str if method == "POST" else query
            prehash = ts + self.api_key + self.RECV_WINDOW + payload
            sign = hmac.new(self.api_secret.encode("utf-8"),
                            prehash.encode("utf-8"), hashlib.sha256).hexdigest()
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": self.RECV_WINDOW,
                "X-BAPI-SIGN": sign,
            })

        try:
            resp = self.session.request(method, url, data=body_str or None,
                                        headers=headers, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise BybitAPIError("NETWORK", str(exc))

        try:
            data = resp.json()
        except ValueError:
            raise BybitAPIError("HTTP", "non-JSON response: %.200s" % resp.text,
                                resp.status_code)

        if data.get("retCode") != 0:
            raise BybitAPIError(str(data.get("retCode")),
                                data.get("retMsg", "unknown error"),
                                resp.status_code)
        return data.get("result") or {}

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------
    @staticmethod
    def spot_symbol(base, quote):
        """"XLM", "USDT" -> "XLMUSDT" (Bybit spot symbol format)."""
        return (base + quote).upper()

    def get_server_time(self):
        """Server time as epoch milliseconds (result.timeSecond is seconds)."""
        result = self._request("GET", "/v5/market/time")
        return int(result["timeSecond"]) * 1000

    def get_symbol_info(self, symbol):
        """Normalized instrument info: status / minTradeUSDT / quotePrecision.

        Bybit reports quotePrecision as a step size ("0.0000001"); we convert
        it to a decimal-place count. minOrderAmt is already quote-denominated.
        """
        result = self._request("GET", "/v5/market/instruments-info",
                               params={"category": "spot", "symbol": symbol})
        items = result.get("list") or []
        if not items:
            raise BybitAPIError("SYMBOL", "unknown spot symbol: %s" % symbol)
        item = items[0]
        lot = item.get("lotSizeFilter") or {}
        info = dict(item)
        info["status"] = "online" if item.get("status") == "Trading" else item.get("status")
        info["minTradeUSDT"] = lot.get("minOrderAmt")
        info["quotePrecision"] = self._step_decimals(lot.get("quotePrecision"))
        return info

    @staticmethod
    def _step_decimals(step):
        """Step size string ("0.0001") -> decimal places (4); "1" -> 0."""
        text = str(step or "1")
        return len(text.split(".", 1)[1].rstrip("0")) if "." in text else 0

    def get_ticker_price(self, symbol):
        """Last traded price for a spot symbol, as float."""
        result = self._request("GET", "/v5/market/tickers",
                               params={"category": "spot", "symbol": symbol})
        items = result.get("list") or []
        if not items:
            raise BybitAPIError("SYMBOL", "no ticker for symbol: %s" % symbol)
        return float(items[0]["lastPrice"])

    # ------------------------------------------------------------------
    # Signed endpoints
    # ------------------------------------------------------------------
    def get_spot_available(self, coin):
        """Spendable spot balance for a coin (0.0 if the coin is absent).

        UTA (accountType=UNIFIED): availableToWithdraw is deprecated since
        2025-01-09, so spendable = walletBalance - locked. Falls back to
        accountType=SPOT (legacy classic accounts, coin[].free) if UNIFIED
        is rejected by the venue.
        """
        try:
            result = self._request("GET", "/v5/account/wallet-balance",
                                   params={"accountType": "UNIFIED", "coin": coin},
                                   signed=True)
        except BybitAPIError as exc:
            if exc.code in ("NETWORK", "HTTP"):
                raise
            result = self._request("GET", "/v5/account/wallet-balance",
                                   params={"accountType": "SPOT", "coin": coin},
                                   signed=True)
        accounts = result.get("list") or []
        coins = accounts[0].get("coin") or [] if accounts else []
        for entry in coins:
            if entry.get("coin", "").upper() != coin.upper():
                continue
            for field in ("free", "availableToWithdraw"):
                value = entry.get(field)
                if value not in (None, ""):
                    return float(value)
            return float(entry.get("walletBalance") or 0) - float(entry.get("locked") or 0)
        return 0.0

    def place_market_buy(self, symbol, quote_amount, client_oid=None):
        """Spot market buy spending `quote_amount` of the QUOTE coin.

        marketUnit=quoteCoin makes qty explicitly quote-denominated (it is
        also Bybit's default for spot market buys). Returns result with
        "orderId" (and echoed "orderLinkId").
        """
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": str(quote_amount),
            "marketUnit": "quoteCoin",
        }
        if client_oid:
            body["orderLinkId"] = _OID_ALLOWED.sub("", str(client_oid))[:36]
        return self._request("POST", "/v5/order/create", body=body, signed=True)

    def get_order_info(self, symbol, order_id):
        """Normalized order state, or None if the order cannot be found.

        /v5/order/realtime keeps only the most recent closed orders; if it
        comes back empty we fall back to /v5/order/history.
        """
        params = {"category": "spot", "symbol": symbol, "orderId": order_id}
        items = (self._request("GET", "/v5/order/realtime",
                               params=params, signed=True).get("list") or [])
        if not items:
            items = (self._request("GET", "/v5/order/history",
                                   params=params, signed=True).get("list") or [])
        if not items:
            return None
        order = items[0]
        raw = order.get("orderStatus", "")
        return {
            "status": _STATUS_MAP.get(raw, raw),
            "baseVolume": float(order.get("cumExecQty") or 0),
            "quoteVolume": float(order.get("cumExecValue") or 0),
        }
