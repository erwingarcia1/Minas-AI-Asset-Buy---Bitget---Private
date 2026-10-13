from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from guardian.exchange import BinanceClient, BitgetClient, BybitClient, OKXClient, make_client
from guardian.poller import _load_client
from tests.guardian.conftest import TEST_USER_ID


def test_make_client_dispatches_by_exchange():
    with patch("guardian.exchange.ccxt.bitget"), patch("guardian.exchange.ccxt.bybit"):
        assert isinstance(make_client("bitget", "k", "s", "p"), BitgetClient)
        assert isinstance(make_client("bybit", "k", "s"), BybitClient)


def test_make_client_rejects_unknown_exchange():
    with pytest.raises(ValueError):
        make_client("kraken", "k", "s")


def test_bybit_client_has_no_passphrase_and_ignores_one():
    with patch("guardian.exchange.ccxt.bybit") as mock_bybit:
        BybitClient("k", "s")
        # constructed without a password field
        args, kwargs = mock_bybit.call_args
        assert "password" not in args[0]


def test_bybit_fetch_closed_orders_iterates_categories():
    with patch("guardian.exchange.ccxt.bybit") as mock_bybit:
        ex = MagicMock()
        ex.fetch_closed_orders.side_effect = lambda **kw: [{"symbol": str(kw.get("params"))}]
        mock_bybit.return_value = ex
        client = BybitClient("k", "s")
        orders = client.fetch_closed_orders()
        # one call per product category (spot, linear, inverse, option)
        assert ex.fetch_closed_orders.call_count == 4
        assert len(orders) == 4


def test_bybit_fetch_balance_falls_back_across_wallet_types():
    with patch("guardian.exchange.ccxt.bybit") as mock_bybit:
        ex = MagicMock()
        calls = {"n": 0}

        def balance(params=None):
            calls["n"] += 1
            if params.get("type") == "unified":
                raise RuntimeError("not a UTA account")
            return {"total": {"USDT": 1}, "type": params["type"]}

        ex.fetch_balance.side_effect = balance
        mock_bybit.return_value = ex
        client = BybitClient("k", "s")
        result = client.fetch_balance()
        assert result["type"] == "spot"  # unified failed, fell back to spot


def test_okx_account_info_normalizes_raw_envelope():
    # Exercises the REAL parsing of OKX's {code,msg,data:[{...}]} envelope,
    # including the 'ip' (singular) -> 'ips' normalization the drift detector
    # depends on.
    with patch("guardian.exchange.ccxt.okx") as mock_okx:
        ex = MagicMock()
        ex.private_get_account_config.return_value = {
            "code": "0", "msg": "",
            "data": [{"uid": "1", "perm": "read_only", "ip": "54.168.212.44"}],
        }
        mock_okx.return_value = ex
        info = OKXClient("k", "s", "p").fetch_account_info()
    assert info["data"]["authorities"] == "read_only"
    assert info["data"]["ips"] == "54.168.212.44"


def test_binance_closed_orders_covers_btc_hints_and_sold_out_coins():
    with patch("guardian.exchange.ccxt.binance") as mock_binance:
        ex = MagicMock()
        ex.fetch_balance.return_value = {"total": {"BTC": 1, "USDT": 50}}
        ex.load_markets.return_value = {"BTC/USDT": {}, "BTC/FDUSD": {}, "XRP/USDT": {}, "ETH/USDT": {}}
        ex.fetch_closed_orders.side_effect = lambda symbol=None, since=None: [{"symbol": symbol}]
        mock_binance.return_value = ex
        client = BinanceClient("k", "s")
        # ETH/USDT: known traded pair; XRP: held last poll, sold to zero since.
        orders = client.fetch_closed_orders(symbol_hints=["ETH/USDT"], coin_hints=["XRP"])
    queried = {o["symbol"] for o in orders}
    # BTC is a quote coin but must still be covered as a base (BTC sells).
    assert "BTC/USDT" in queried
    assert "ETH/USDT" in queried  # baseline pair hint
    assert "XRP/USDT" in queried  # sold-to-zero coin hint
    # nonexistent markets are filtered out, not queried
    assert "XRP/FDUSD" not in queried


def test_binance_transfers_query_all_routes_and_filter_by_coin():
    with patch("guardian.exchange.ccxt.binance") as mock_binance:
        ex = MagicMock()

        def transfers(code=None, since=None, params=None):
            return [{
                "id": f"{params['fromAccount']}-{params['toAccount']}",
                "currency": "USDT",
                "fromAccount": params["fromAccount"], "toAccount": params["toAccount"],
            }]

        ex.fetch_transfers.side_effect = transfers
        mock_binance.return_value = ex
        client = BinanceClient("k", "s")
        usdt = client.fetch_transfers("USDT")
        # ccxt's default is spot->future ONLY; the wrapper must sweep both
        # directions of spot<->funding/future/margin.
        assert ex.fetch_transfers.call_count == 6
        routes = {(t["fromAccount"], t["toAccount"]) for t in usdt}
        assert ("spot", "funding") in routes and ("funding", "spot") in routes
        # second coin reuses the per-instance cache and filters locally
        assert client.fetch_transfers("BTC") == []
        assert ex.fetch_transfers.call_count == 6


def test_load_client_uses_row_exchange(db, cipher):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, label, exchange, topology, encrypted_api_key, "
            "encrypted_secret, encrypted_passphrase, onboarded_at, updated_at) VALUES "
            "(%s, 'self', 'bb', 'bybit', 'independent', %s, %s, %s, '2026-01-01', '2026-01-01')",
            (TEST_USER_ID, cipher.encrypt("k"), cipher.encrypt("s"), cipher.encrypt("")),
        )
        cur.execute("SELECT * FROM guardian_accounts WHERE label='bb'")
        row = cur.fetchone()
    with patch("guardian.exchange.ccxt.bybit"):
        client = _load_client(cipher, row)
    assert isinstance(client, BybitClient)
