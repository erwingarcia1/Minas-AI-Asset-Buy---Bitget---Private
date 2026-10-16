"""Sample CCXT-unified response shapes used across tests. Fixed values only -
tests never hit a live exchange."""

from __future__ import annotations


def withdrawal_record(
    order_id="wd-1",
    amount="0.5",
    currency="BTC",
    status="pending",
    address="bc1qexistingaddress",
    timestamp=1700000000000,
):
    return {
        "id": order_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "address": address,
        "timestamp": timestamp,
        "fee": {"currency": currency, "cost": "0.0001"},
        "info": {},
    }


def deposit_record(order_id="dep-1", amount="1.0", currency="BTC", address="bc1qexistingaddress"):
    return {
        "id": order_id,
        "amount": amount,
        "currency": currency,
        "status": "success",
        "address": address,
        "timestamp": 1699000000000,
        "info": {},
    }


def trade_record(symbol="BTC/USDT", side="sell", amount="0.1", cost="6000", fee_currency="USDT", fee_cost="6"):
    return {
        "symbol": symbol,
        "side": side,
        "amount": amount,
        "cost": cost,
        "fee": {"currency": fee_currency, "cost": fee_cost},
        "timestamp": 1700000000000,
    }


def account_info_response(authorities="spot_account_r,spot_order_r", ips="1.2.3.4"):
    return {
        "code": "00000",
        "msg": "success",
        "data": {
            "userId": "12345",
            "authorities": authorities,
            "ips": ips,
            "parentId": None,
            "traderType": "0",
        },
    }
