"""Wallet address validation: Bitcoin (P2PKH / P2SH / Segwit, mainnet + testnet),
Ethereum, Solana, network inference and address/network matching."""
import pytest

from api.utils.network_validator import (
    detect_chain, get_address_network, validate_bitcoin_address,
    validate_ethereum_address, validate_solana_address)

BTC_P2PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BTC_P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
BTC_SEGWIT = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
BTC_TAPROOT = "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"
BTC_TEST_P2PKH = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn"
BTC_TEST_P2SH = "2MzQwSSnBHWHqSAqtTVQ6v47XtaisrJa1Vc"
BTC_TEST_SEGWIT = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
ETH = "0x52908400098527886E0F7030069857D2E4169EE7"
ETH_LOWER = "0x52908400098527886e0f7030069857d2e4169ee7"
SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


class TestBitcoin:
    @pytest.mark.parametrize("addr", [BTC_P2PKH, BTC_P2SH, BTC_SEGWIT, BTC_TAPROOT])
    def test_valid_mainnet_formats(self, addr):
        assert validate_bitcoin_address(addr)
        assert get_address_network(addr) == "mainnet"

    @pytest.mark.parametrize("addr", [BTC_TEST_P2PKH, BTC_TEST_P2SH, BTC_TEST_SEGWIT])
    def test_valid_testnet_formats(self, addr):
        assert validate_bitcoin_address(addr)
        assert get_address_network(addr) == "testnet"

    @pytest.mark.parametrize("addr", [
        BTC_P2PKH[:-1] + "b",            # checksum broken (last char changed)
        BTC_P2SH[:-2] + "xx",
        BTC_SEGWIT[:-1] + "x",           # bech32 checksum broken
        "bc1" + "q" * 20,                # bad bech32 payload
        "bc1Qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",  # mixed case is invalid bech32
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfN0",          # '0' is not base58
        "", "1", "3", "bc1", None,
    ])
    def test_invalid(self, addr):
        assert not validate_bitcoin_address(addr)
        assert get_address_network(addr) is None

    def test_not_confused_with_other_chains(self):
        assert not validate_bitcoin_address(ETH)
        assert not validate_bitcoin_address(SOL)


class TestEthereum:
    def test_valid_checksummed_and_lowercase(self):
        assert validate_ethereum_address(ETH)
        assert validate_ethereum_address(ETH_LOWER)
        assert validate_ethereum_address("0x" + "F" * 40)

    @pytest.mark.parametrize("addr", [
        "52908400098527886E0F7030069857D2E4169EE7",   # no 0x
        "0x52908400098527886E0F7030069857D2E4169EE",   # 39 hex
        "0x52908400098527886E0F7030069857D2E4169EE77", # 41 hex
        "0xG2908400098527886E0F7030069857D2E4169EE7",  # non-hex
        "0X52908400098527886E0F7030069857D2E4169EE7",  # uppercase X
        "", None, BTC_P2PKH, SOL,
    ])
    def test_invalid(self, addr):
        assert not validate_ethereum_address(addr)


class TestSolana:
    def test_valid(self):
        assert validate_solana_address(SOL)
        assert validate_solana_address("11111111111111111111111111111111")  # system program

    @pytest.mark.parametrize("addr", [
        SOL + "A",            # 33 bytes
        SOL[:-4],             # too short
        "0" + SOL[1:],        # '0' not in base58 alphabet
        ETH, BTC_P2PKH, "", None,
    ])
    def test_invalid(self, addr):
        assert not validate_solana_address(addr)


class TestNetworkInference:
    def test_detect_chain(self):
        assert detect_chain(BTC_SEGWIT) == "bitcoin"
        assert detect_chain(BTC_TEST_P2PKH) == "bitcoin"
        assert detect_chain(ETH) == "ethereum"
        assert detect_chain(SOL) == "solana"
        assert detect_chain("nope") is None

    def test_mainnet_vs_testnet_detection(self):
        assert get_address_network(BTC_P2PKH) == "mainnet"
        assert get_address_network(BTC_TEST_P2PKH) == "testnet"
        assert get_address_network(BTC_TEST_SEGWIT) == "testnet"
        # Ethereum / Solana addresses carry no network information -> mainnet by convention
        assert get_address_network(ETH) == "mainnet"
        assert get_address_network(SOL) == "mainnet"
        assert get_address_network("garbage") is None


def test_supported_purchase_coins_map_to_an_address_family():
    """USDT is the quote coin, not a purchase coin (kept from tests/test_exchange_networks.py)."""
    from api.utils import exchange_networks as en
    assert set(en.SUPPORTED_CRYPTOS) == {"BTC", "ETH", "USDC", "SOL", "XRP", "XLM"}
    assert en.CRYPTO_CHAINS["XRP"] == "xrpl" and en.CRYPTO_CHAINS["XLM"] == "stellar"
