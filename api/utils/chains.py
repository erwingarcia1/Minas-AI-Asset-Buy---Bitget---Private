"""Exchange chain names (as Bitget reports them in /api/v2/spot/public/coins)
mapped to the address family we can validate, plus the legacy
mainnet/testnet -> chain mapping used by migration 018.

A purchase or wallet stores the exchange's own chain string
(``blockchain_network`` = "BTC", "ERC20", "SOL", "TRC20", ...); the address
format is checked against the chain's family. Chains whose address format we
cannot validate are refused for withdrawals.
"""
from __future__ import annotations

from typing import Optional

from api.utils.network_validator import (detect_chain, get_address_network, validate_bitcoin_address,
                                         validate_ethereum_address, validate_solana_address,
                                         validate_tron_address)

# Bitget chain name (case-insensitive) -> address family
CHAIN_FAMILIES = {
    "btc": "bitcoin",
    "erc20": "ethereum", "bep20": "ethereum", "arbitrumone": "ethereum", "arbitrum": "ethereum",
    "optimism": "ethereum", "polygon": "ethereum", "base": "ethereum", "avaxc-chain": "ethereum",
    "avaxc": "ethereum", "zksyncera": "ethereum", "linea": "ethereum", "scroll": "ethereum",
    "opbnb": "ethereum", "mantle": "ethereum", "morph": "ethereum", "plasma": "ethereum",
    "sol": "solana",
    "trc20": "tron",
}

# What the coin is delivered as when the user picks the "native" chain.
DEFAULT_CHAIN_FOR_COIN = {"BTC": "BTC", "ETH": "ERC20", "USDT": "ERC20", "USDC": "ERC20", "SOL": "SOL"}

FAMILY_LABELS = {"bitcoin": "Bitcoin", "ethereum": "EVM (0x…)", "solana": "Solana", "tron": "Tron (T…)"}


def normalize_chain(chain: Optional[str]) -> Optional[str]:
    return chain.strip() if chain else None


def family_for_chain(chain: Optional[str]) -> Optional[str]:
    """Address family for an exchange chain name, or None when unknown."""
    if not chain:
        return None
    return CHAIN_FAMILIES.get(chain.strip().lower())


def is_legacy_network(value: Optional[str]) -> bool:
    return (value or "").lower() in ("mainnet", "testnet")


def chain_for_legacy(coin: str, network: str) -> str:
    """Migration helper: the chain a legacy mainnet/testnet purchase maps to."""
    return DEFAULT_CHAIN_FOR_COIN.get((coin or "").upper(), "ERC20")


def validate_address_for_chain(address: str, chain: str) -> tuple[bool, str]:
    """(ok, reason). Checks the address format against the chain's family;
    Bitcoin addresses must be mainnet (exchanges withdraw to mainnet only)."""
    family = family_for_chain(chain)
    if family is None:
        return False, f"chain {chain!r} is not supported for withdrawals by this platform yet"
    a = (address or "").strip()
    if family == "bitcoin":
        if not validate_bitcoin_address(a):
            return False, "not a valid Bitcoin address"
        if get_address_network(a) != "mainnet":
            return False, "Bitcoin testnet addresses cannot receive exchange withdrawals"
        return True, "valid Bitcoin mainnet address"
    if family == "ethereum":
        return (True, f"valid EVM address for {chain}") if validate_ethereum_address(a) else (False, f"{chain} needs a 0x… address")
    if family == "solana":
        return (True, "valid Solana address") if validate_solana_address(a) else (False, "not a valid Solana address")
    if family == "tron":
        return (True, "valid Tron address") if validate_tron_address(a) else (False, "TRC20 needs a T… Tron address")
    return False, f"unsupported address family {family}"


def guess_chain_for_address(address: str, coin: Optional[str] = None) -> Optional[str]:
    """Migration/import helper: a plausible chain for an address (by format)."""
    fam = detect_chain(address)
    if fam == "bitcoin":
        return "BTC"
    if fam == "ethereum":
        return "ERC20"
    if fam == "solana":
        return "SOL"
    if validate_tron_address(address):
        return "TRC20"
    return DEFAULT_CHAIN_FOR_COIN.get((coin or "").upper())
