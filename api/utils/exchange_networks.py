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

# Coin -> chain whose address format applies. USDT/USDC are delivered as
# ERC-20 tokens, so they use Ethereum addresses.
CRYPTO_CHAINS: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "ethereum",
    "USDC": "ethereum",
    "SOL": "solana",
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
