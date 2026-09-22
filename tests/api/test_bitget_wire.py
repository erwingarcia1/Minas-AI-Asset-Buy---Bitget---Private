"""The exact requests Bitget receives from ExchangeClient(exchange="bitget").

Captured at the HTTP boundary (requests.Session.request), so this pins method,
path, query, JSON body and signed headers (minus the per-call signature and
timestamp) for every call the executor makes. Written before audit Batch C1
removed the Bybit/OKX branches; it must pass unchanged before and after.
"""
import json
from unittest import mock

import pytest

from api.services.exchange_client import ExchangeClient

OK = {"code": "00000", "msg": "success"}
ORDER = {"orderId": "1484219600489320448", "clientOid": "ab-1", "status": "filled", "baseVolume": "0.000197",
         "quoteVolume": "14.99812614", "priceAvg": "76132.62", "feeDetail": "", "fee": "", "feeCoin": ""}
SYMBOL = {"symbol": "BTCUSDT", "status": "online", "minTradeUSDT": "1", "quantityPrecision": "6",
          "pricePrecision": "2", "takerFeeRate": "0.002"}
CHAINS = {"coin": "XRP", "chains": [{"chain": "XRP", "needTag": "true", "withdrawable": "true", "withdrawFee": "0.2",
                                     "minWithdrawAmount": "5", "withdrawMinScale": "6"}]}
WD_ROWS = [{"orderId": "wd-1", "clientOid": "abw-1", "status": "success", "tradeId": "0xhash", "fee": "0.2"}]


class Response:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data
        self.text = json.dumps({**OK, "data": data})

    def json(self):
        return {**OK, "data": self._data}


@pytest.fixture
def wire(monkeypatch):
    """Records every request; answers with canned Bitget payloads by path."""
    calls = []
    answers = {"/api/v2/spot/public/symbols": [SYMBOL], "/api/v2/spot/trade/place-order": ORDER,
               "/api/v2/spot/trade/orderInfo": [ORDER], "/api/v2/spot/public/coins": [CHAINS],
               "/api/v2/spot/wallet/withdrawal": {"orderId": "wd-1", "clientOid": "abw-1"},
               "/api/v2/spot/wallet/withdrawal-records": WD_ROWS}

    def fake_request(self, method, url, data=None, headers=None, timeout=None, **kw):
        path, _, query = url.replace("https://api.bitget.com", "").partition("?")
        signed = {k: v for k, v in (headers or {}).items() if k not in ("ACCESS-SIGN", "ACCESS-TIMESTAMP")}
        calls.append({"method": method, "path": path, "query": query, "body": json.loads(data) if data else None,
                      "headers": signed})
        return Response(answers[path])
    monkeypatch.setattr("requests.Session.request", fake_request)
    return calls


AUTH = {"Content-Type": "application/json", "locale": "en-US", "ACCESS-KEY": "key-1", "ACCESS-PASSPHRASE": "pass-1"}
PUBLIC = {"Content-Type": "application/json", "locale": "en-US"}


def client():
    return ExchangeClient("key-1", "secret-1", passphrase="pass-1", exchange="bitget")


def test_market_buy_wire(wire):
    out = client().place_buy("BTCUSDT", 15.0, "ab-1", dry_run=False)
    assert out == {"order_id": "1484219600489320448", "dry_run": False}
    assert wire == [
        {"method": "GET", "path": "/api/v2/spot/public/symbols", "query": "symbol=BTCUSDT", "body": None, "headers": PUBLIC},
        {"method": "POST", "path": "/api/v2/spot/trade/place-order", "query": "",
         "body": {"symbol": "BTCUSDT", "side": "buy", "orderType": "market", "force": "gtc", "size": "15.0", "clientOid": "ab-1"},
         "headers": AUTH},
    ]


def test_order_lookup_by_client_oid_wire(wire):
    out = client().lookup_order("BTCUSDT", "ab-1")
    assert out["state"] == "filled" and out["fill"]["amount_crypto"] == 0.000197
    assert wire == [{"method": "GET", "path": "/api/v2/spot/trade/orderInfo", "query": "clientOid=ab-1", "body": None, "headers": AUTH}]


def test_fetch_fill_by_order_id_wire(wire):
    out = client().fetch_fill("BTCUSDT", 15.0, "1484219600489320448", "ab-1", dry_run=False)
    assert out["filled"] and out["cost_usdt"] == 14.99812614
    assert wire == [{"method": "GET", "path": "/api/v2/spot/trade/orderInfo", "query": "orderId=1484219600489320448", "body": None, "headers": AUTH}]


def test_coin_info_wire(wire):
    chains = client().get_coin_chains("XRP")
    assert chains[0]["needTag"] == "true"
    assert wire == [{"method": "GET", "path": "/api/v2/spot/public/coins", "query": "coin=XRP", "body": None, "headers": PUBLIC}]


def test_withdraw_with_tag_wire(wire):
    out = client().withdraw("XRP", "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePL", 6.0, network="XRP", client_oid="abw-1",
                            dry_run=False, tag="12345")
    assert out == {"withdrawal_id": "wd-1", "amount": 6.0, "dry_run": False}
    assert wire == [{"method": "POST", "path": "/api/v2/spot/wallet/withdrawal", "query": "",
                     "body": {"coin": "XRP", "transferType": "on_chain", "address": "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePL",
                              "chain": "XRP", "size": "6.0", "tag": "12345", "clientOid": "abw-1"},
                     "headers": AUTH}]


def test_withdraw_without_tag_wire(wire):
    client().withdraw("BTC", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", 0.001, network="BTC", client_oid="abw-2", dry_run=False,
                      travel_rule={"first_name": "Ada", "last_name": "Lovelace"})
    assert wire == [{"method": "POST", "path": "/api/v2/spot/wallet/withdrawal", "query": "",
                     "body": {"coin": "BTC", "transferType": "on_chain", "address": "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
                              "chain": "BTC", "size": "0.001", "clientOid": "abw-2", "firstName": "Ada", "lastName": "Lovelace"},
                     "headers": AUTH}]
    assert "tag" not in wire[0]["body"]


def test_withdrawal_record_lookup_wire(wire):
    out = client().withdrawal_status("XRP", "abw-1", "wd-1", 1700000000000, 1700003600000)
    assert out["found"] and out["raw_status"] == "success" and out["tx_hash"] == "0xhash" and out["fee"] == 0.2
    assert wire == [{"method": "GET", "path": "/api/v2/spot/wallet/withdrawal-records",
                     "query": "clientOid=abw-1&coin=XRP&endTime=1700003600000&limit=100&orderId=wd-1&startTime=1700000000000",
                     "body": None, "headers": AUTH}]


def test_dry_run_sends_nothing(wire):
    c = client()
    c.place_buy("BTCUSDT", 15.0, "ab-1", dry_run=True)
    c.withdraw("XRP", "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePL", 6.0, network="XRP", dry_run=True, tag="12345")
    assert wire == []
