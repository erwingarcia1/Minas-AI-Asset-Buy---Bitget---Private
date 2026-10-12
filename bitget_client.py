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

    def get_ticker(self, symbol):
        """Live ticker: {"last", "ask", "bid", "ts"} (floats / epoch ms), straight from
        /api/v2/spot/market/tickers with no rounding."""
        data = self._request("GET", "/api/v2/spot/market/tickers", params={"symbol": symbol}, auth=False)
        if not data:
            raise BitgetAPIError("SYMBOL", f"no ticker for {symbol!r}")
        t = data[0]

        def num(key):
            v = t.get(key)
            return float(v) if v not in (None, "") else None
        return {"last": num("lastPr"), "ask": num("askPr"), "bid": num("bidPr"), "ts": t.get("ts")}

    def get_candles(self, symbol, granularity, limit=100, end_time_ms=None, history=False):
        """Public OHLCV candles for a spot symbol, oldest first:
        [[ts_ms, open, high, low, close, base_vol, quote_vol, usdt_vol], ...] as strings.
        /api/v2/spot/market/candles serves recent data (up to 1000 rows);
        /api/v2/spot/market/history-candles serves older data (up to 200 rows,
        needs endTime). Granularities include 15min, 1h, 4h, 1day."""
        params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
        if end_time_ms is not None:
            params["endTime"] = str(int(end_time_ms))
        path = "/api/v2/spot/market/history-candles" if history else "/api/v2/spot/market/candles"
        return self._request("GET", path, params=params, auth=False) or []

    def get_ticker_price(self, symbol):
        price = self.get_ticker(symbol)["last"]
        if price is None:
            raise BitgetAPIError("SYMBOL", f"ticker for {symbol!r} carries no last price")
        return price

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

    def get_order_info(self, symbol, order_id=None, client_oid=None):
        """Normalized order lookup by Bitget order id or by our clientOid:
        {"status", "orderId", "clientOid", "baseVolume", "quoteVolume", "priceAvg",
         "fee", "feeCoin", "feeDetail"} or None when Bitget has no such order.

        Bitget statuses: live | partially_filled | filled | cancelled. Fees come
        from feeDetail (a JSON string keyed by fee coin; totals are negative).
        `symbol` is unused (Bitget resolves orders by id alone) but kept for
        interface parity with the other exchange clients.
        """
        params = {"orderId": str(order_id)} if order_id else {"clientOid": str(client_oid)}
        try:
            data = self._request("GET", "/api/v2/spot/trade/orderInfo", params=params)
        except BitgetAPIError as exc:
            # 43001 "order does not exist" family: an honest "not found" is not a transport error
            if str(exc.code) in ("43001", "43025", "40109") or "not exist" in str(exc.message).lower():
                return None
            raise
        if not data:
            return None
        order = data[0]
        fee, fee_coin = self._parse_fee_detail(order.get("feeDetail"))
        return {
            "status": str(order.get("status", "")).lower(),
            "orderId": str(order.get("orderId", "")),
            "clientOid": order.get("clientOid"),
            "baseVolume": order.get("baseVolume"),
            "quoteVolume": order.get("quoteVolume"),
            "priceAvg": order.get("priceAvg"),
            "fee": fee,
            "feeCoin": fee_coin,
            "feeDetail": order.get("feeDetail"),
        }

    @staticmethod
    def _parse_fee_detail(raw):
        """(total fee as a positive float, fee coin) from Bitget's feeDetail JSON string,
        e.g. '{"BTC":{"deduction":false,"feeCoinCode":"BTC","totalDeductionFee":0,"totalFee":-0.000001}}'.
        Returns (None, None) when absent or unparsable."""
        if not raw:
            return None, None
        try:
            detail = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            return None, None
        best = None
        for coin, entry in detail.items():
            if not isinstance(entry, dict):
                continue
            total = entry.get("totalFee")
            try:
                amount = abs(float(total)) if total not in (None, "") else None
            except (TypeError, ValueError):
                amount = None
            if amount is None:
                continue
            coin_code = entry.get("feeCoinCode") or coin
            if best is None or amount > best[0]:
                best = (amount, str(coin_code))
        return best if best else (None, None)

    def get_fills(self, symbol, order_id):
        """Trade fills for an order: list of {"tradeId", "priceAvg", "size", "amount", "feeDetail"}."""
        data = self._request("GET", "/api/v2/spot/trade/fills", params={"symbol": symbol, "orderId": str(order_id)})
        return data or []

    # -------------------------------------------------------------- wallet

    def get_coin_info(self, coin):
        """Chains for a coin from /api/v2/spot/public/coins (no auth): list of
        {"chain", "withdrawable", "withdrawFee", "extraWithdrawFee", "minWithdrawAmount",
         "withdrawMinScale", "needTag", "contractAddress", "browserUrl", "withdrawConfirm"}."""
        data = self._request("GET", "/api/v2/spot/public/coins", params={"coin": coin}, auth=False)
        if not data:
            raise BitgetAPIError("SYMBOL", f"unknown coin {coin!r}")
        return data[0].get("chains") or []

    # Optional travel-rule / beneficiary fields Bitget accepts on a withdrawal,
    # keyed by our snake_case names.
    TRAVEL_RULE_FIELDS = {"first_name": "firstName", "last_name": "lastName", "identity_type": "identityType",
                          "company_name": "companyName", "area_code": "areaCode", "member_code": "memberCode"}

    def withdraw(self, coin, address, amount, network=None, client_oid=None, tag=None, remark=None,
                 travel_rule=None):
        """On-chain withdrawal (POST /api/v2/spot/wallet/withdrawal). Needs the
        Withdraw permission, a whitelisted IP and the address in the account's
        address book. `travel_rule` (dict, see TRAVEL_RULE_FIELDS) is sent when
        given. Returns Bitget's withdrawal orderId."""
        body = {"coin": coin, "transferType": "on_chain", "address": address, "chain": network,
                "size": str(amount)}
        if tag:
            # Destination tag / memo for XRP, XLM and similar. Field name "tag" comes from
            # Bitget's v2 withdrawal docs and SDK; NOT yet verified against a real tagged
            # withdrawal (no live one has been sent as of 2026-09-16).
            body["tag"] = str(tag)
        if remark:
            body["remark"] = remark
        if client_oid:
            body["clientOid"] = client_oid
        for ours, theirs in self.TRAVEL_RULE_FIELDS.items():
            if (travel_rule or {}).get(ours):
                body[theirs] = str(travel_rule[ours])
        data = self._request("POST", "/api/v2/spot/wallet/withdrawal", body=body) or {}
        return str(data.get("orderId") or "")

    def get_withdrawal_records(self, coin, start_ms, end_ms, client_oid=None, order_id=None, limit=100):
        """Withdrawal records (GET /api/v2/spot/wallet/withdrawal-records): list of
        {"orderId", "tradeId" (on-chain tx hash), "coin", "chain", "size", "fee",
         "status" (wallet_processing | success | wallet_fail | cancel | reject ...),
         "toAddress", "confirm", "cTime", "uTime"}."""
        params = {"coin": coin, "startTime": str(start_ms), "endTime": str(end_ms), "limit": str(limit)}
        if client_oid:
            params["clientOid"] = client_oid
        if order_id:
            params["orderId"] = str(order_id)
        return self._request("GET", "/api/v2/spot/wallet/withdrawal-records", params=params) or []


# ---------------------------------------------------------------------------
# Unified Trading Account (UTA) client, Bitget API v3 (v0.8.0). The Classic client
# above is untouched: this subclass overrides only the calls whose path or shape
# changed. Signing and transport are identical (same headers, same prehash).
# Field names from Bitget's UTA docs, read 2026-10-08:
#   POST /api/v3/trade/place-order    {category, symbol, side, orderType, qty, clientOid}; qty of a
#                                     market buy is in the QUOTE coin (USDT), like v2 size
#   GET  /api/v3/trade/order-info  orderId | clientOid -> orderStatus (live|new|partially_filled|
#                                     filled|cancelled), cumExecQty (base), cumExecValue (quote),
#                                     avgPrice, feeDetail [{feeCoin, fee}]
#   GET  /api/v3/trade/fills          category, orderId -> execPrice, execQty, execValue, feeDetail
#   GET  /api/v3/account/assets       per coin: available ("Transferable amount"), locked, balance,
#                                     equity, debt; account totals are never used
#   GET  /api/v3/account/info         permType (read-only | read-and-write), permissions []
#   Classic account on a v3 call -> error 40084; Unified account on a v2 call -> 40085.
# ---------------------------------------------------------------------------
UTA_CLASSIC_ONLY_CODE = "40085"   # the Classic (v2) API refused: the account is Unified
UTA_UNIFIED_ONLY_CODE = "40084"   # the Unified (v3) API refused: the account is Classic
UTA_ORDER_STATUS = {"filled": "filled", "cancelled": "cancelled", "partially_filled": "partially_filled",
                    "live": "live", "new": "new"}
UTA_ORDER_NOT_EXIST_CODE = "25204"   # v3 "Order does not exist": the honest "not found", like the v2 43001 family


class BitgetUnifiedClient(BitgetClient):
    """Same credentials, same signing; Unified (v3) endpoints for account, order and fills."""
    account_mode = "unified"

    def get_account_info(self):
        """{"user_id", "ips", "authorities", "perm_type", "permissions"}: v3 account info.
        ``authorities`` carries the v3 permission list so callers can keep one shape."""
        data = self._request("GET", "/api/v3/account/info") or {}
        perms = [str(p) for p in (data.get("permissions") or [])]
        return {"user_id": str(data.get("userId") or ""), "ips": str(data.get("ips") or ""),
                "authorities": perms, "perm_type": str(data.get("permType") or ""), "permissions": perms,
                "parent_id": str(data.get("parentId") or "")}

    def get_spot_available(self, coin):
        """Available (transferable) `coin` in the unified account minus any debt in that coin.
        Never equity or account totals (owner's decision, 2026-10-08)."""
        data = self._request("GET", "/api/v3/account/assets", params={"coin": coin}) or {}
        rows = data.get("assets") if isinstance(data, dict) else data
        for row in rows or []:
            if str(row.get("coin", "")).upper() == coin.upper():
                available = float(row.get("available") or 0)
                debt = float(row.get("debt") or 0)
                return max(available - debt, 0.0)
        return 0.0

    def place_market_buy(self, symbol, quote_amount, client_oid=None):
        """Market-buy `symbol` spending `quote_amount` USDT (v3: qty is the quote amount for a market buy).
        Returns the order as get_order_info shapes it, so the caller sees the same keys as v2."""
        body = {"category": "SPOT", "symbol": symbol, "side": "buy", "orderType": "market", "qty": str(quote_amount)}
        if client_oid:
            body["clientOid"] = client_oid
        placed = self._request("POST", "/api/v3/trade/place-order", body=body) or {}
        order_id = placed.get("orderId")
        info = self.get_order_info(symbol, order_id=order_id, client_oid=client_oid) or {}
        return {"orderId": order_id, "clientOid": placed.get("clientOid") or client_oid,
                "executedQty": info.get("baseVolume") or "0", "cummulativeQuoteQty": info.get("quoteVolume") or "0",
                "status": info.get("status"), "priceAvg": info.get("priceAvg"), "fee": info.get("fee"), "feeCoin": info.get("feeCoin")}

    def get_order_info(self, symbol, order_id=None, client_oid=None):
        """Same keys as the Classic client: {status, orderId, clientOid, baseVolume, quoteVolume, priceAvg,
        fee, feeCoin, feeDetail}, or None when Bitget has no such order."""
        params = {"orderId": str(order_id)} if order_id else {"clientOid": str(client_oid)}
        try:
            data = self._request("GET", "/api/v3/trade/order-info", params=params)
        except BitgetAPIError as exc:
            # 25204 "Order does not exist": an honest "not found" is not a transport error,
            # exactly like the Classic client's 43001 family (seen live on the 2026-10-10 PEPE park)
            if str(exc.code) == UTA_ORDER_NOT_EXIST_CODE or "not exist" in str(exc.message).lower():
                return None
            raise
        order = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) and data else None)
        if not order:
            return None
        fee, fee_coin = self._parse_fee_list(order.get("feeDetail"))
        return {"status": UTA_ORDER_STATUS.get(str(order.get("orderStatus") or "").lower(), str(order.get("orderStatus") or "")),
                "orderId": str(order.get("orderId") or ""), "clientOid": order.get("clientOid"),
                "baseVolume": order.get("cumExecQty"), "quoteVolume": order.get("cumExecValue"), "priceAvg": order.get("avgPrice"),
                "fee": fee, "feeCoin": fee_coin, "feeDetail": order.get("feeDetail")}

    @staticmethod
    def _parse_fee_list(raw):
        """(total fee as a positive float, fee coin) from the v3 feeDetail list [{feeCoin, fee}]; fees may be negative."""
        if not raw:
            return None, None
        total, coin = 0.0, None
        for item in raw if isinstance(raw, list) else []:
            try:
                total += abs(float(item.get("fee") or 0))
            except (TypeError, ValueError):
                continue
            coin = coin or item.get("feeCoin")
        return (total if coin else None), coin

    def get_fills(self, symbol, order_id):
        """Trade fills for an order, Classic shape: list of {"tradeId", "priceAvg", "size", "amount", "feeDetail"}."""
        data = self._request("GET", "/api/v3/trade/fills", params={"category": "SPOT", "orderId": str(order_id)}) or {}
        rows = data.get("list") if isinstance(data, dict) else data
        return [{"tradeId": r.get("execId"), "priceAvg": r.get("execPrice"), "size": r.get("execQty"),
                 "amount": r.get("execValue"), "feeDetail": r.get("feeDetail")} for r in rows or []]

    # Every v3 path this client calls, with a harmless read-only request for each (place-order excluded):
    # a made-up order id must get Bitget's normal "no such order" answer, never 40404 (unknown URL).
    PROBE_PATHS = (
        ("GET", "/api/v3/account/info", {}),
        ("GET", "/api/v3/account/assets", {"coin": "USDT"}),
        ("GET", "/api/v3/trade/order-info", {"clientOid": "ab-probe-000000000000000000"}),
        ("GET", "/api/v3/trade/fills", {"category": "SPOT", "orderId": "1"}),
    )
    URL_NOT_FOUND = "40404"

    def probe_endpoints(self):
        """[(path, ok, note)]: ok is False only when Bitget answers 40404 (the path does not exist).
        A normal answer, an empty list or a 'not found'/'parameter' error all mean the path is live."""
        out = []
        for method, path, params in self.PROBE_PATHS:
            try:
                data = self._request(method, path, params=params or None)
                out.append((path, True, "ok" if data else "empty"))
            except BitgetAPIError as exc:
                code = str(getattr(exc, "code", ""))
                out.append((path, code != self.URL_NOT_FOUND, f"error {code}: {str(exc)[:60]}"))
            except Exception as exc:   # noqa: BLE001
                out.append((path, False, f"{type(exc).__name__}: {str(exc)[:60]}"))
        return out

    def withdraw(self, *a, **k):
        raise BitgetAPIError("UTA_WITHDRAW", "withdrawals from a Unified Trading Account are not supported by AutoBuy yet")

    def get_withdrawal_records(self, *a, **k):
        raise BitgetAPIError("UTA_WITHDRAW", "withdrawal records of a Unified Trading Account are not supported by AutoBuy yet")
