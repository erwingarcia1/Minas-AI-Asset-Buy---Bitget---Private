from __future__ import annotations

from unittest.mock import MagicMock, patch

from guardian.exchange import BinanceClient, OKXClient, make_client
from guardian.onboarding import _binance_read_only, _okx_read_only, verify_read_only

# ---------------- OKX gate ----------------

def test_okx_read_only_accepted():
    ok, _ = _okx_read_only({"perm": "read_only"})
    assert ok


def test_okx_trade_rejected():
    ok, reason = _okx_read_only({"perm": "read_only,trade"})
    assert not ok and "trade" in reason


def test_okx_withdraw_rejected():
    ok, reason = _okx_read_only({"perm": "read_only,withdraw"})
    assert not ok and "withdraw" in reason


def test_okx_empty_perm_fails_closed():
    ok, reason = _okx_read_only({"perm": ""})
    assert not ok and "failing closed" in reason


def test_okx_missing_perm_fails_closed():
    ok, _ = _okx_read_only({})
    assert not ok


def test_okx_unknown_token_fails_closed():
    ok, reason = _okx_read_only({"perm": "read_only,superuser"})
    assert not ok and "unrecognized" in reason


# ---------------- Binance gate ----------------

def _binance_perms(**overrides):
    base = {
        "enableReading": True,
        "enableWithdrawals": False, "enableInternalTransfer": False, "enableMargin": False,
        "enableFutures": False, "permitsUniversalTransfer": False, "enableVanillaOptions": False,
        "enableFixApiTrade": False, "enableSpotAndMarginTrading": False,
        "enablePortfolioMarginTrading": False,
    }
    base.update(overrides)
    return base


def test_binance_read_only_accepted():
    ok, _ = _binance_read_only(_binance_perms())
    assert ok


def test_binance_ignores_benign_flags():
    # enableFixReadOnly / ipRestrict true must not cause rejection
    ok, _ = _binance_read_only(_binance_perms(enableFixReadOnly=True, ipRestrict=True))
    assert ok


def test_binance_spot_trading_rejected():
    ok, reason = _binance_read_only(_binance_perms(enableSpotAndMarginTrading=True))
    assert not ok and "enableSpotAndMarginTrading" in reason


def test_binance_withdrawals_rejected():
    ok, reason = _binance_read_only(_binance_perms(enableWithdrawals=True))
    assert not ok and "enableWithdrawals" in reason


def test_binance_portfolio_margin_rejected():
    ok, _ = _binance_read_only(_binance_perms(enablePortfolioMarginTrading=True))
    assert not ok


def test_binance_reading_disabled_rejected():
    ok, reason = _binance_read_only(_binance_perms(enableReading=False))
    assert not ok and "enableReading" in reason


def test_binance_missing_write_flag_fails_closed():
    perms = _binance_perms()
    del perms["enableFutures"]
    ok, reason = _binance_read_only(perms)
    assert not ok and "missing" in reason


# ---------------- dispatch + client wiring ----------------

def _mock_client_with_info(data):
    client = MagicMock()
    client.fetch_balance.return_value = {"total": {"USDT": 1}}
    client.fetch_account_info.return_value = {"data": data}
    return client


def test_verify_dispatch_okx():
    ok, _ = verify_read_only(_mock_client_with_info({"perm": "read_only"}), exchange="okx")
    assert ok
    ok, _ = verify_read_only(_mock_client_with_info({"perm": "read_only,trade"}), exchange="okx")
    assert not ok


def test_verify_dispatch_binance():
    ok, _ = verify_read_only(_mock_client_with_info(_binance_perms()), exchange="binance")
    assert ok
    ok, _ = verify_read_only(_mock_client_with_info(_binance_perms(enableFutures=True)), exchange="binance")
    assert not ok


def test_make_client_okx_binance():
    with patch("guardian.exchange.ccxt.okx"), patch("guardian.exchange.ccxt.binance"):
        assert isinstance(make_client("okx", "k", "s", "p"), OKXClient)
        assert isinstance(make_client("binance", "k", "s"), BinanceClient)


def test_okx_requires_passphrase_binance_does_not():
    with patch("guardian.exchange.ccxt.okx") as mok, patch("guardian.exchange.ccxt.binance") as mbn:
        OKXClient("k", "s", "pass")
        assert mok.call_args[0][0].get("password") == "pass"
        BinanceClient("k", "s")
        assert "password" not in mbn.call_args[0][0]
