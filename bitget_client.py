"""Minimal signed REST client for the Bitget v2 API.

Implements the ACCESS-SIGN scheme documented at
https://www.bitget.com/api-doc/common/signature :
sign = base64( HMAC-SHA256( secret, timestamp + METHOD + requestPath + body ) )
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

import requests

from exchange_errors import ExchangeAPIError

log = logging.getLogger("bitget")

BASE_URL = "https://api.bitget.com"


class BitgetAPIError(ExchangeAPIError):
    """Raised when Bitget returns a non-00000 business code or an HTTP error."""

    exchange = "Bitget"


class BitgetClient:
    def __init__(self, api_key="", api_secret="", passphrase="", base_url=BASE_URL, timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------ auth

    def _sign(self, timestamp, method, request_path, body):
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(self, method, path, params=None, body=None, auth=True):
        query = f"?{urlencode(sorted(params.items()))}" if params else ""
        request_path = path + query
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            timestamp = str(int(time.time() * 1000))
            headers.update(
                {
                    "ACCESS-KEY": self.api_key,
                    "ACCESS-SIGN": self._sign(timestamp, method, request_path, body_str),
                    "ACCESS-TIMESTAMP": timestamp,
                    "ACCESS-PASSPHRASE": self.passphrase,
                }
            )

        url = self.base_url + request_path
        try:
            resp = self.session.request(
                method,
                url,
                data=body_str if body is not None else None,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise BitgetAPIError("NETWORK", f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError:
            raise BitgetAPIError("HTTP", f"non-JSON response ({resp.status_code}): {resp.text[:200]}", resp.status_code)

        code = str(payload.get("code", ""))
        if resp.status_code != 200 or code != "00000":
            raise BitgetAPIError(code or str(resp.status_code), payload.get("msg", resp.text[:200]), resp.status_code)
        return payload.get("data")

    # --------------------------------------------------------------- public

    @staticmethod
    def spot_symbol(base, quote):
        """Bitget spot symbol format: BTC + USDT -> BTCUSDT."""
        return f"{base}{quote}"

    def get_server_time(self):
        """Bitget server time in epoch milliseconds. No auth required."""
        data = self._request("GET", "/api/v2/public/time", auth=False)
        return int(data["serverTime"])

    def get_symbol_info(self, symbol):
        """Spot symbol metadata (min order size, precisions, status)."""
        data = self._request("GET", "/api/v2/spot/public/symbols", params={"symbol": symbol}, auth=False)
        if not data:
            raise BitgetAPIError("SYMBOL", f"unknown spot symbol {symbol!r}")
        return data[0]

    def get_ticker_price(self, symbol):
        data = self._request("GET", "/api/v2/spot/market/tickers", params={"symbol": symbol}, auth=False)
        if not data:
            raise BitgetAPIError("SYMBOL", f"no ticker for {symbol!r}")
        return float(data[0]["lastPr"])

    # -------------------------------------------------------------- private

    def get_account_info(self):
        """Account info for the key owner: ``{"user_id", "ips", "authorities"}``.

        A harmless authenticated call (no balances, no orders) that proves the
        key, secret, passphrase and IP allowlist are all accepted. ``authorities``
        is Bitget's list of the key's permissions (e.g. "readonly", "trade",
        "withdraw"); empty when the venue does not report it.
        """
        data = self._request("GET", "/api/v2/spot/account/info") or {}
        auths = data.get("authorities") or []
        if isinstance(auths, str):
            auths = [a for a in auths.replace(";", ",").split(",") if a]
        return {"user_id": str(data.get("userId", "")), "ips": data.get("ips"),
                "authorities": [str(a) for a in auths]}

    def get_spot_available(self, coin):
        """Available (free) spot balance of `coin` for the account owning this API key."""
        data = self._request("GET", "/api/v2/spot/account/assets", params={"coin": coin})
        for entry in data or []:
            if entry.get("coin", "").upper() == coin.upper():
                return float(entry.get("available", "0"))
        return 0.0

    def place_market_buy(self, symbol, quote_amount, client_oid=None):
        """Market-buy `symbol` spending `quote_amount` of the quote coin (USDT).

        For Bitget spot market buys, `size` is denominated in the quote currency.
        """
        body = {
            "symbol": symbol,
            "side": "buy",
            "orderType": "market",
            "force": "gtc",
            "size": str(quote_amount),
        }
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", "/api/v2/spot/trade/place-order", body=body)

    def get_order_info(self, symbol, order_id):
        """Normalized order lookup: {"status", "baseVolume", "quoteVolume"} or None.

        `symbol` is unused (Bitget resolves orders by id alone) but kept for
        interface parity with the other exchange clients.
        """
        data = self._request("GET", "/api/v2/spot/trade/orderInfo", params={"orderId": order_id})
        if not data:
            return None
        order = data[0]
        return {
            "status": str(order.get("status", "")).lower(),
            "baseVolume": order.get("baseVolume"),
            "quoteVolume": order.get("quoteVolume"),
        }
