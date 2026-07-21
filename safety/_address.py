"""Address validation for the safety gate -- stdlib only, no new dependencies.

EVM  : EIP-55 mixed-case checksum -- https://eips.ethereum.org/EIPS/eip-55
       Requires keccak-256 (NOT hashlib.sha3_256, which is FIPS SHA3 with a
       different padding byte), so a compact keccak-f[1600] lives here.
Solana: base58-encoded 32-byte mint pubkey.

Fail closed: anything we cannot positively validate is rejected.
"""

from __future__ import annotations

import re

_MASK = (1 << 64) - 1

# Keccak-f[1600] round constants and rotation offsets r[x][y].
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ---------- keccak-256 ----------

def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _keccak_f(a):
    for rnd in range(24):
        # theta
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        # rho + pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & _MASK & b[(x + 2) % 5][y])
        # iota
        a[0][0] ^= _RC[rnd]
    return a


def keccak256(data: bytes) -> bytes:
    """Original Keccak-256 (0x01 domain byte), as used by Ethereum."""
    rate = 136  # 1088 bits
    buf = bytearray(data)
    buf.append(0x01)
    while len(buf) % rate:
        buf.append(0x00)
    buf[-1] |= 0x80

    state = [[0] * 5 for _ in range(5)]
    for off in range(0, len(buf), rate):
        block = buf[off:off + rate]
        for i in range(rate // 8):
            state[i % 5][i // 5] ^= int.from_bytes(block[i * 8:i * 8 + 8], "little")
        _keccak_f(state)

    out = bytearray()
    for i in range(4):  # 4 lanes = 32 bytes
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


# ---------- public ----------

def to_checksum_address(address: str) -> str:
    """Return the EIP-55 mixed-case form of a 0x-prefixed EVM address."""
    if not _EVM_RE.match(address or ""):
        raise ValueError("not a 20-byte hex address")
    body = address[2:].lower()
    digest = keccak256(body.encode("ascii")).hex()
    return "0x" + "".join(
        ch.upper() if ch.isalpha() and int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(body)
    )


def is_checksum_address(address: str) -> bool:
    """True only for a syntactically valid, correctly EIP-55 checksummed address.

    All-lowercase addresses are REJECTED on purpose: a checksum is the only
    cheap defence against a typo'd or homoglyph-swapped address reaching a
    money-moving call, and the allowlist stores checksummed forms.
    """
    try:
        return to_checksum_address(address) == address
    except (ValueError, TypeError):
        return False


def b58decode(value: str) -> bytes:
    """Decode base58 (Bitcoin/Solana alphabet). Raises ValueError on bad input."""
    if not value:
        raise ValueError("empty base58 string")
    num = 0
    for ch in value:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base58 character {ch!r}")
        num = num * 58 + idx
    pad = len(value) - len(value.lstrip("1"))
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * pad + body


def is_valid_solana_mint(address: str) -> bool:
    """True when the string is base58 that decodes to exactly 32 bytes."""
    if not address or not (32 <= len(address) <= 44):
        return False
    try:
        return len(b58decode(address)) == 32
    except (ValueError, TypeError):
        return False


__all__ = [
    "keccak256",
    "to_checksum_address",
    "is_checksum_address",
    "b58decode",
    "is_valid_solana_mint",
]
