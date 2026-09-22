"""Wallet address validation for Bitcoin, Ethereum and Solana, with mainnet /
testnet inference. Pure functions, no network access, stdlib only.

Bitcoin: P2PKH (1…) and P2SH (3…) are Base58Check-verified; Segwit (bc1…) is
Bech32/Bech32m-verified. Testnet equivalents (m/n…, 2…, tb1…) are recognised
so a testnet address can never be accepted as mainnet.
Ethereum: 0x + 40 hex characters (EIP-55 checksum is not enforced because the
same address is valid on every EVM network; case is preserved as given).
Solana: Base58 that decodes to exactly 32 bytes.

Ethereum and Solana addresses are network-agnostic (the same key works on
mainnet and testnet), so they validate for either declared network.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

__all__ = ["validate_bitcoin_address", "validate_ethereum_address", "validate_solana_address",
           "validate_tron_address", "get_address_network", "detect_chain"]

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_ETH_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST, _BECH32M_CONST = 1, 0x2BC830A3


# ----------------------------------------------------------------- base58

def _b58decode(s: str) -> Optional[bytes]:
    if not s or any(c not in _B58_INDEX for c in s):
        return None
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def _b58check_payload(s: str) -> Optional[bytes]:
    """Version byte + payload when the 4-byte double-SHA256 checksum matches."""
    raw = _b58decode(s)
    if raw is None or len(raw) < 5:
        return None
    data, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4] != checksum:
        return None
    return data


# ----------------------------------------------------------------- bech32

def _bech32_polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _bech32_decode(addr: str) -> Optional[tuple[str, int, bytes]]:
    """(hrp, witness_version, program) or None. Accepts bech32 (v0) and bech32m (v1+)."""
    if not (14 <= len(addr) <= 90) or addr != addr.lower() and addr != addr.upper():
        return None
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr):
        return None
    hrp, data_part = addr[:pos], addr[pos + 1:]
    if any(c not in _BECH32_CHARSET for c in data_part):
        return None
    data = [_BECH32_CHARSET.index(c) for c in data_part]
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    const = _bech32_polymod(hrp_exp + data)
    version, payload = data[0], data[1:-6]
    if version == 0 and const != _BECH32_CONST:
        return None
    if version != 0 and const != _BECH32M_CONST:
        return None
    if version > 16:
        return None
    # convert 5-bit groups to bytes
    acc = bits = 0
    out = bytearray()
    for v in payload:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or (acc << (8 - bits)) & 0xFF:
        return None
    if not (2 <= len(out) <= 40) or (version == 0 and len(out) not in (20, 32)):
        return None
    return hrp, version, bytes(out)


# ----------------------------------------------------------------- public API

def _bitcoin_network(address: str) -> Optional[str]:
    """'mainnet' / 'testnet' for a valid Bitcoin address, else None."""
    if not isinstance(address, str) or not address:
        return None
    lower = address.lower()
    if lower.startswith(("bc1", "tb1", "bcrt1")):
        decoded = _bech32_decode(address)
        if decoded is None:
            return None
        hrp = decoded[0]
        return "mainnet" if hrp == "bc" else "testnet" if hrp in ("tb", "bcrt") else None
    data = _b58check_payload(address)
    if data is None or len(data) != 21:
        return None
    version = data[0]
    if version in (0x00, 0x05):     # P2PKH "1…", P2SH "3…"
        return "mainnet"
    if version in (0x6F, 0xC4):     # testnet P2PKH "m/n…", P2SH "2…"
        return "testnet"
    return None


def validate_bitcoin_address(address: str) -> bool:
    """True for a checksum-valid Bitcoin address: P2PKH (1…), P2SH (3…) or Segwit (bc1…),
    or their testnet forms (m/n…, 2…, tb1…)."""
    return _bitcoin_network(address) is not None


def validate_ethereum_address(address: str) -> bool:
    """True for ``0x`` followed by exactly 40 hexadecimal characters."""
    return isinstance(address, str) and bool(_ETH_RE.match(address))


def validate_solana_address(address: str) -> bool:
    """True for a Base58 string that decodes to a 32-byte public key."""
    if not isinstance(address, str) or not (32 <= len(address) <= 44):
        return False
    raw = _b58decode(address)
    return raw is not None and len(raw) == 32


def validate_tron_address(address: str) -> bool:
    """True for a Base58Check Tron address: version byte 0x41, 21 bytes, starts with 'T'."""
    if not isinstance(address, str) or not address.startswith("T") or len(address) != 34:
        return False
    data = _b58check_payload(address)
    return data is not None and len(data) == 21 and data[0] == 0x41


def detect_chain(address: str) -> Optional[str]:
    """'bitcoin' | 'ethereum' | 'solana' for a valid address, else None.
    Bitcoin is checked before Solana because base58 alone cannot tell them apart."""
    if validate_ethereum_address(address):
        return "ethereum"
    if validate_bitcoin_address(address):
        return "bitcoin"
    if validate_solana_address(address):
        return "solana"
    return None


def get_address_network(address: str) -> Optional[str]:
    """Infer 'mainnet' or 'testnet' from the address format.

    Only Bitcoin encodes the network in the address. Ethereum and Solana
    addresses are identical on every network, so they report 'mainnet' (the
    conventional default). Returns None for an invalid address.
    """
    chain = detect_chain(address)
    if chain == "bitcoin":
        return _bitcoin_network(address)
    if chain in ("ethereum", "solana"):
        return "mainnet"
    return None
