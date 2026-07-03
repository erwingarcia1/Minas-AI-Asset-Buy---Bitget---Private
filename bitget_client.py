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

log = logging.getLogger("bitget")

BASE_URL = "https://api.bitget.com"


class BitgetAPIError(Exception):
    """Raised when Bitget returns a non-00000 business code or an HTTP error."""

    def __init__(self, code, message, http_status=None):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(f"Bitget error {code}: {message}")


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
        resp = self.session.request(
            method,
            url,
            data=body_str if body is not None else None,
            headers=headers,
            timeout=self.timeout,
        )

        try:
            payload = resp.json()
        except ValueError:
            raise BitgetAPIError("HTTP", f"non-JSON response ({resp.status_code}): {resp.text[:200]}", resp.status_code)

        code = str(payload.get("code", ""))
        if resp.status_code != 200 or code != "00000":
            raise BitgetAPIError(code or str(resp.status_code), payload.get("msg", resp.text[:200]), resp.status_code)
        return payload.get("data")

    # --------------------------------------------------------------- public

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

    def get_order_info(self, order_id):
        data = self._request("GET", "/api/v2/spot/trade/orderInfo", params={"orderId": order_id})
        return data[0] if data else None
