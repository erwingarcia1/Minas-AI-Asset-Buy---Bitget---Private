import pytest

import api.services.exchange_client as bc_mod
from api.services.exchange_client import ExchangeClient, ExchangeClientError
from exchange_errors import ExchangeAPIError


class FakeLegacy:
    """Scriptable stand-in for the legacy client."""
    def __init__(self, **kw):
        self.orders = []
        self.price = 50000.0
        self.symbol_status = "online"
        self.min_notional = "5"
        self.fail_with = None

    def get_ticker_price(self, symbol):
        if self.fail_with:
            raise self.fail_with
        return self.price

    def get_symbol_info(self, symbol):
        return {"status": self.symbol_status, "minTradeUSDT": self.min_notional,
                "quotePrecision": 8}

    def place_market_buy(self, symbol, quote_amount, client_oid=None):
        self.orders.append((symbol, quote_amount, client_oid))
        return {"orderId": 987654, "executedQty": "0.00200000",
                "cummulativeQuoteQty": "100.00000000"}


@pytest.fixture
def legacy(monkeypatch):
    fake = FakeLegacy()
    monkeypatch.setattr(bc_mod, "_LegacyClient", lambda **kw: fake)
    return fake


def make():
    return ExchangeClient("k", "s")


def test_price_fetch(legacy):
    assert make().get_price("BTCUSDT") == 50000.0


def test_price_error_normalized(legacy):
    legacy.fail_with = ExchangeAPIError("-1121", "Invalid symbol.")
    with pytest.raises(ExchangeClientError):
        make().get_price("NOPEUSDT")


def test_dry_run_places_nothing(legacy):
    order = make().execute_order("BTCUSDT", 100.0, dry_run=True)
    assert order["order_id"].startswith("DRY-")
    assert order["amount_crypto"] == pytest.approx(0.002)
    assert order["exchange_rate"] == 50000.0
    assert legacy.orders == [], "dry run must not reach place_market_buy"


def test_live_order_uses_fill_math(legacy):
    order = make().execute_order("BTCUSDT", 100.0, dry_run=False, client_oid="oid-1")
    assert order["order_id"] == "987654"
    assert order["amount_crypto"] == pytest.approx(0.002)
    assert order["exchange_rate"] == pytest.approx(50000.0)   # quote/base from fills
    assert legacy.orders == [("BTCUSDT", 100.0, "oid-1")]


def test_live_order_rejects_below_min_notional(legacy):
    legacy.min_notional = "500"
    with pytest.raises(ExchangeClientError, match="below the 500"):
        make().execute_order("BTCUSDT", 100.0, dry_run=False)
    assert legacy.orders == []


def test_live_order_rejects_halted_symbol(legacy):
    legacy.symbol_status = "HALT"
    with pytest.raises(ExchangeClientError, match="not trading"):
        make().execute_order("BTCUSDT", 100.0, dry_run=False)


def test_network_error_normalized(legacy):
    legacy.fail_with = ExchangeAPIError("HTTP", "connection reset")
    with pytest.raises(ExchangeClientError, match="connection reset"):
        make().execute_order("BTCUSDT", 100.0, dry_run=False)
