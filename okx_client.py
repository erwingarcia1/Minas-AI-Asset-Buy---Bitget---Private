"""Minimal signed REST client for the OKX v5 API.

Implements the OK-ACCESS-SIGN scheme documented at
https://www.okx.com/docs-v5/en/#overview-rest-authentication :
sign = base64( HMAC-SHA256( secret, timestamp + METHOD + requestPath + body ) )
where requestPath INCLUDES the ?query string and the timestamp is ISO8601 UTC
with millisecond precision, e.g. 2020-12-08T09:08:57.715Z.

Success convention: top-level code == "0"; order endpoints ALSO carry a
per-item sCode inside data[] — sCode != "0" is an error even when the
HTTP layer succeeded.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from exchange_errors import ExchangeAPIError

log = logging.getLogger("okx")

BASE_URL = "https://www.okx.com"

# clOrdId: case-sensitive alphanumerics only, up to 32 characters.
_CLIENT_OID_BAD_CHARS = re.compile(r"[^A-Za-z0-9]")
_CLIENT_OID_MAX_LEN = 32

# OKX order states -> the bot's normalized lowercase states.
_ORDER_STATUS_MAP = {
    "live": "live",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "cancelled",
    "mmp_canceled": "cancelled",
}

_INSTRUMENT_NOT_FOUND = "51001"  # "Instrument ID doesn't exist"
_ORDER_NOT_FOUND = "51603"       # "Order does not exist"


class OKXAPIError(ExchangeAPIError):
    """Raised when OKX returns a non-0 code/sCode or the HTTP request fails."""

    exchange = "OKX"


class OKXClient:
    def __init__(self, api_key="", api_secret="", passphrase="", base_url=BASE_URL, timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------ auth

    @staticmethod
    def _timestamp():
        """ISO8601 UTC with milliseconds, e.g. 2020-12-08T09:08:57.715Z."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, timestamp, method, request_path, body):
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(self, method, path, params=None, body=None, auth=True):
        # requestPath used in the signature includes the ?query string.
        query = f"?{urlencode(sorted(params.items()))}" if params else ""
        request_path = path + query
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers = {"Content-Type": "application/json"}
        if auth:
            timestamp = self._timestamp()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body_str),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
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
            raise OKXAPIError("NETWORK", f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError:
            raise OKXAPIError("HTTP", f"non-JSON response ({resp.status_code}): {resp.text[:200]}", resp.status_code)

        data = payload.get("data") or []
        # Per-item sCode (order endpoints) is more specific than the generic
        # top-level "1" that accompanies it, so surface it first.
        for item in data:
            if isinstance(item, dict) and item.get("sCode") not in (None, "", "0"):
                raise OKXAPIError(item["sCode"], item.get("sMsg") or payload.get("msg", ""), resp.status_code)
        code = str(payload.get("code", ""))
        if resp.status_code != 200 or code != "0":
            raise OKXAPIError(code or str(resp.status_code), payload.get("msg", resp.text[:200]), resp.status_code)
        return data

    # --------------------------------------------------------------- public

    @staticmethod
    def spot_symbol(base, quote):
        """OKX spot instId format: ("XLM", "USDT") -> "XLM-USDT"."""
        return f"{base.upper()}-{quote.upper()}"

    def get_server_time(self):
        """OKX server time in epoch milliseconds. No auth required."""
        data = self._request("GET", "/api/v5/public/time", auth=False)
        return int(data[0]["ts"])

    def get_symbol_info(self, symbol):
        """Spot instrument metadata normalized to {status, minTradeUSDT, quotePrecision}."""
        try:
            data = self._request(
                "GET", "/api/v5/public/instruments",
                params={"instType": "SPOT", "instId": symbol}, auth=False)
        except OKXAPIError as exc:
            if exc.code == _INSTRUMENT_NOT_FOUND:
                raise OKXAPIError("SYMBOL", f"unknown spot symbol {symbol!r}", exc.http_status) from exc
            raise
        if not data:
            raise OKXAPIError("SYMBOL", f"unknown spot symbol {symbol!r}")
        info = data[0]

        # OKX minSz is denominated in the BASE currency for spot, so the
        # minimum quote value is approximated as minSz * last price. The true
        # floor moves with the market; callers should keep some headroom.
        min_trade_usdt = float(info.get("minSz") or 0) * self.get_ticker_price(symbol)

        # OKX has no explicit quote-amount precision field; use the decimal
        # places of tickSz (e.g. "0.0001" -> 4) as a conservative proxy.
        tick = info.get("tickSz", "0.0001")
        quote_precision = len(tick.split(".")[1].rstrip("0")) if "." in tick else 0

        return {
            "status": "online" if info.get("state") == "live" else info.get("state"),
            "minTradeUSDT": min_trade_usdt,
            "quotePrecision": quote_precision,
        }

    def get_ticker_price(self, symbol):
        data = self._request("GET", "/api/v5/market/ticker", params={"instId": symbol}, auth=False)
        if not data:
            raise OKXAPIError("SYMBOL", f"no ticker for {symbol!r}")
        return float(data[0]["last"])

    # -------------------------------------------------------------- private

    def get_spot_available(self, coin):
        """Available (free) balance of `coin` in the TRADING account.

        NOTE: OKX deposits land in the FUNDING account by default. Funds must
        be auto-transferred / swept into the Trading account before they show
        up here — otherwise the bot never sees the deposit.
        """
        data = self._request("GET", "/api/v5/account/balance", params={"ccy": coin.upper()})
        details = data[0].get("details") or [] if data else []
        for entry in details:
            if entry.get("ccy", "").upper() == coin.upper():
                return float(entry.get("availBal") or 0)
        return 0.0

    @staticmethod
    def _sanitize_client_oid(client_oid):
        """Clamp a caller id to OKX's clOrdId rules: alphanumeric, max 32 chars."""
        cleaned = _CLIENT_OID_BAD_CHARS.sub("", client_oid or "")
        return cleaned[:_CLIENT_OID_MAX_LEN] or None

    def place_market_buy(self, symbol, quote_amount, client_oid=None):
        """Market-buy `symbol` spending `quote_amount` of the quote coin (USDT).

        tgtCcy="quote_ccy" makes `sz` quote-denominated (also the OKX default
        for spot market buys, but pinned explicitly so it can never flip).
        """
        body = {
            "instId": symbol,
            "tdMode": "cash",
            "side": "buy",
            "ordType": "market",
            "sz": str(quote_amount),
            "tgtCcy": "quote_ccy",
        }
        oid = self._sanitize_client_oid(client_oid) if client_oid else None
        if oid:
            body["clOrdId"] = oid
        data = self._request("POST", "/api/v5/trade/order", body=body)
        order = data[0]
        return {"orderId": order["ordId"], "clientOid": order.get("clOrdId", "")}

    def get_order_info(self, symbol, order_id):
        """Order state normalized to {status, baseVolume, quoteVolume}, or None if unknown."""
        try:
            data = self._request("GET", "/api/v5/trade/order",
                                 params={"instId": symbol, "ordId": order_id})
        except OKXAPIError as exc:
            if exc.code == _ORDER_NOT_FOUND:
                return None
            raise
        if not data:
            return None
        order = data[0]
        status = order.get("state", "")
        base_volume = float(order.get("accFillSz") or 0)
        avg_px = float(order.get("avgPx") or 0)
        return {
            "status": _ORDER_STATUS_MAP.get(status, status),
            "baseVolume": base_volume,
            # OKX spot order details expose accFillSz (base) and avgPx but no
            # direct fill-notional field, so quoteVolume is accFillSz * avgPx.
            "quoteVolume": base_volume * avg_px,
        }
