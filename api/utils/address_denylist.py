"""Addresses that must never be accepted as a destination: provably
unspendable, burn and placeholder addresses. Checked on every purchase (both
modes), on the switch to withdraw-immediately, and on saved wallets.

Bitcoin Base58 addresses are case-sensitive; EVM addresses are compared
case-insensitively; Solana addresses are case-sensitive Base58.
"""
from __future__ import annotations

DENYLISTED_ADDRESSES = {
    # Bitcoin
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",   # genesis block coinbase (unspendable)
    "1111111111111111111114oLvT2",           # all-zero pubkey hash (burn)
    "1BitcoinEaterAddressDontSendf59kuE",    # well-known burn vanity address
    "1CounterpartyXXXXXXXXXXXXXXXUXCHtA",    # Counterparty proof-of-burn
    "bc1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
    # Ethereum / EVM (compared lower-cased)
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000001",
    "0xffffffffffffffffffffffffffffffffffffffff",
    # Solana
    "11111111111111111111111111111111",       # system program
    "1nc1nerator11111111111111111111111111111111",
}

_EVM_LOWER = {a.lower() for a in DENYLISTED_ADDRESSES if a.startswith("0x")}
_EXACT = {a for a in DENYLISTED_ADDRESSES if not a.startswith("0x")}


def is_denylisted(address: str | None) -> bool:
    """True when the address is a known unspendable/burn/placeholder address."""
    if not address:
        return False
    a = address.strip()
    return a in _EXACT or (a.lower().startswith("0x") and a.lower() in _EVM_LOWER)


DENYLIST_REASON = ("This is a known unspendable, burn or placeholder address; coins sent there are lost. "
                   "Enter a wallet you control.")
