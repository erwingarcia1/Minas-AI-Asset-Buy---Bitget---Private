import pytest

from api.utils import exchange_networks as en


@pytest.mark.parametrize("exchange", ["binance", "bitget", "okx", "bybit"])
def test_each_exchange_supports_mainnet_and_testnet(exchange):
    assert en.supported_networks(exchange) == ["mainnet", "testnet"]
    assert en.can_exchange_withdraw_to_network(exchange, "mainnet")
    assert en.can_exchange_withdraw_to_network(exchange, "testnet")


def test_exchange_and_network_are_case_insensitive():
    assert en.can_exchange_withdraw_to_network("Binance", "MAINNET")
    assert en.can_exchange_withdraw_to_network("OKX", "Testnet")


def test_unknown_exchange_or_network_rejected():
    assert not en.can_exchange_withdraw_to_network("kraken", "mainnet")
    assert not en.can_exchange_withdraw_to_network("binance", "devnet")
    assert not en.can_exchange_withdraw_to_network("", "mainnet")
    assert not en.can_exchange_withdraw_to_network("binance", "")
    assert en.supported_networks("coinbase") == []


def test_config_changes_are_honoured(monkeypatch):
    monkeypatch.setitem(en.EXCHANGE_NETWORKS, "bybit", ["mainnet"])
    assert en.can_exchange_withdraw_to_network("bybit", "mainnet")
    assert not en.can_exchange_withdraw_to_network("bybit", "testnet"), "unsupported combo must be rejected"


def test_supported_cryptos_map_to_chains():
    assert set(en.SUPPORTED_CRYPTOS) == {"BTC", "ETH", "USDC", "SOL", "XRP", "XLM"}, "USDT is the quote coin, not a purchase coin"
    assert en.chain_for_crypto("btc") == "bitcoin"
    assert en.chain_for_crypto("XRP") == "xrpl" and en.chain_for_crypto("XLM") == "stellar"
    assert en.chain_for_crypto("USDC") == "ethereum"
    assert en.chain_for_crypto("SOL") == "solana"
    assert en.chain_for_crypto("DOGE") is None
