"""Which blockchain networks each supported exchange can withdraw to, and which
chain each supported coin is delivered on."""
from __future__ import annotations

from typing import Dict, List

EXCHANGE_NETWORKS: Dict[str, List[str]] = {
    "binance": ["mainnet", "testnet"],
    "bitget": ["mainnet", "testnet"],
    "okx": ["mainnet", "testnet"],
    "bybit": ["mainnet", "testnet"],
}

# Coin -> chain whose address format applies. USDC is delivered as an ERC-20
# token by default, so it uses Ethereum addresses. USDT is NOT a purchase coin:
# purchases are paid in USDT and Bitget has no USDTUSDT pair. USDC stays because
# USDCUSDT exists and was online on Bitget when checked (2026-09-13).
CRYPTO_CHAINS: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDC": "ethereum",
    "SOL": "solana",
    "XRP": "xrpl",       # native XRP on the XRP Ledger (Bitget chain "XRP"); destination tag on exchanges
    "XLM": "stellar",    # native XLM on Stellar (Bitget chain "StellarLumens"); memo on exchanges
}

SUPPORTED_EXCHANGES = tuple(EXCHANGE_NETWORKS)
SUPPORTED_CRYPTOS = tuple(CRYPTO_CHAINS)


def supported_networks(exchange_name: str) -> List[str]:
    """Networks `exchange_name` can withdraw to (empty list for unknown exchanges)."""
    return list(EXCHANGE_NETWORKS.get((exchange_name or "").lower(), []))


def can_exchange_withdraw_to_network(exchange_name: str, blockchain_network: str) -> bool:
    """True when the exchange is known and supports withdrawals to that network."""
    return (blockchain_network or "").lower() in supported_networks(exchange_name)


def chain_for_crypto(crypto_symbol: str) -> str | None:
    """Address-format chain for a coin symbol, or None if the coin is unsupported."""
    return CRYPTO_CHAINS.get((crypto_symbol or "").upper())
