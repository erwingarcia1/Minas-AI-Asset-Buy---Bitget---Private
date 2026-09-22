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

from api.utils.network_validator import (b58decode, b58encode, detect_chain, double_sha256_checksum_ok, get_address_network,
                                         validate_bitcoin_address,
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
    "xrp": "xrpl",              # Bitget chain name for the XRP Ledger (native XRP); needs a destination tag on exchanges
    "stellarlumens": "stellar", # Bitget chain name for Stellar (native XLM); needs a memo on exchanges
}
TAG_CHAINS = {"xrpl": "destination tag", "stellar": "memo"}   # family -> what the tag is called
DESTINATION_TYPES = ("self_custody", "exchange")

# What the coin is delivered as when the user picks the "native" chain.
DEFAULT_CHAIN_FOR_COIN = {"BTC": "BTC", "ETH": "ERC20", "USDC": "ERC20", "SOL": "SOL", "XRP": "XRP", "XLM": "StellarLumens"}

FAMILY_LABELS = {"bitcoin": "Bitcoin", "ethereum": "EVM (0x…)", "solana": "Solana", "tron": "Tron (T…)",
                 "xrpl": "XRP Ledger (r…)", "stellar": "Stellar (G…)"}


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
    if family == "xrpl":
        parsed = parse_xrp_address(a)
        return (True, "valid XRP Ledger address" + (" (X-address with embedded tag)" if parsed["tag"] is not None else "")) if parsed["ok"] else (False, parsed["reason"])
    if family == "stellar":
        parsed = parse_stellar_address(a)
        return (True, "valid Stellar address" + (" (muxed, memo embedded)" if parsed["tag"] is not None else "")) if parsed["ok"] else (False, parsed["reason"])
    return False, f"unsupported address family {family}"


# --------------------------------------------------------------------------- XRP Ledger

_RIPPLE_B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"


def _b58_decode(text: str, alphabet: str) -> Optional[bytes]:
    return b58decode(text, alphabet)


def _b58_encode(data: bytes, alphabet: str) -> str:
    return b58encode(data, alphabet)


def _xrp_checksum_ok(payload: bytes) -> bool:
    return double_sha256_checksum_ok(payload)


def parse_xrp_address(address: str) -> dict:
    """{ok, reason, classic, tag}. Accepts a classic address (r…, base58 over the
    Ripple alphabet, version 0x00, 20-byte account id, double-SHA256 checksum) or
    an X-address (X…, which embeds the destination tag: version 0x05 0x44 for
    mainnet, account id, tag flag, 8-byte tag). Returns the classic form and the
    embedded tag (None when there is none)."""
    a = (address or "").strip()
    if not a:
        return {"ok": False, "reason": "empty address", "classic": None, "tag": None}
    raw = _b58_decode(a, _RIPPLE_B58)
    if raw is None:
        return {"ok": False, "reason": "not a valid XRP Ledger address (unexpected character)", "classic": None, "tag": None}
    if a.startswith("r"):
        if len(raw) != 25 or raw[0] != 0 or not _xrp_checksum_ok(raw):
            return {"ok": False, "reason": "not a valid XRP Ledger address (bad length or checksum)", "classic": None, "tag": None}
        return {"ok": True, "reason": "valid XRP Ledger address", "classic": a, "tag": None}
    if a.startswith("X"):
        if len(raw) != 35 or raw[:2] != b"\x05\x44" or not _xrp_checksum_ok(raw):
            return {"ok": False, "reason": "not a valid mainnet X-address (bad prefix, length or checksum)", "classic": None, "tag": None}
        account, flag, tag_bytes = raw[2:22], raw[22], raw[23:31]
        tag = int.from_bytes(tag_bytes, "little") if flag == 1 else None
        if flag not in (0, 1):
            return {"ok": False, "reason": "X-address with an unsupported tag flag", "classic": None, "tag": None}
        classic_payload = b"\x00" + account
        import hashlib
        classic = _b58_encode(classic_payload + hashlib.sha256(hashlib.sha256(classic_payload).digest()).digest()[:4], _RIPPLE_B58)
        return {"ok": True, "reason": "valid X-address", "classic": classic, "tag": tag}
    if a.startswith("T"):
        return {"ok": False, "reason": "testnet X-address: exchange withdrawals go to mainnet only", "classic": None, "tag": None}
    return {"ok": False, "reason": "XRP addresses start with r (or X for an X-address)", "classic": None, "tag": None}


def validate_xrp_tag(tag: str) -> tuple[bool, str]:
    t = (tag or "").strip()
    if not t.isdigit() or len(t) > 10 or int(t) > 0xFFFFFFFF:
        return False, "an XRP destination tag is a whole number between 0 and 4294967295"
    return True, "ok"


# --------------------------------------------------------------------------- Stellar

def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def parse_stellar_address(address: str) -> dict:
    """{ok, reason, classic, tag}. Accepts a public key (G…, 56 chars, base32,
    version byte 6<<3, 32-byte key, CRC16-XModem little-endian) or a muxed
    address (M…, 69 chars, version 12<<3, 32-byte key + 8-byte memo id) and
    returns the base G address plus the embedded memo id (None for a plain G)."""
    import base64
    a = (address or "").strip()
    if not a:
        return {"ok": False, "reason": "empty address", "classic": None, "tag": None}
    if a[0] not in "GM":
        return {"ok": False, "reason": "Stellar addresses start with G (or M for a muxed address)", "classic": None, "tag": None}
    expected_len = 56 if a[0] == "G" else 69
    if len(a) != expected_len or a != a.upper() or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in a):
        return {"ok": False, "reason": f"not a valid Stellar address ({expected_len} base32 characters expected)", "classic": None, "tag": None}
    try:
        raw = base64.b32decode(a + "=" * (-len(a) % 8))
    except Exception:
        return {"ok": False, "reason": "not a valid Stellar address (base32 decode failed)", "classic": None, "tag": None}
    body, check = raw[:-2], int.from_bytes(raw[-2:], "little")
    if _crc16_xmodem(body) != check:
        return {"ok": False, "reason": "not a valid Stellar address (checksum mismatch)", "classic": None, "tag": None}
    if a[0] == "G":
        if body[0] != 6 << 3 or len(body) != 33:
            return {"ok": False, "reason": "not a Stellar public key (wrong version byte)", "classic": None, "tag": None}
        return {"ok": True, "reason": "valid Stellar address", "classic": a, "tag": None}
    if body[0] != 12 << 3 or len(body) != 41:
        return {"ok": False, "reason": "not a valid muxed Stellar address (wrong version byte)", "classic": None, "tag": None}
    key, memo_id = body[1:33], int.from_bytes(body[33:41], "big")
    g_body = bytes([6 << 3]) + key
    classic = base64.b32encode(g_body + _crc16_xmodem(g_body).to_bytes(2, "little")).decode()
    return {"ok": True, "reason": "valid muxed Stellar address", "classic": classic, "tag": str(memo_id)}


def validate_stellar_memo(memo: str) -> tuple[bool, str]:
    m = (memo or "").strip()
    if not m:
        return False, "a Stellar memo cannot be empty"
    if m.isdigit():
        return (True, "ok") if len(m) <= 20 and int(m) <= 0xFFFFFFFFFFFFFFFF else (False, "a Stellar id memo must fit in 64 bits")
    return (True, "ok") if len(m.encode()) <= 28 else (False, "a Stellar text memo is at most 28 bytes")


def parse_tagged_address(address: str, chain: str) -> dict:
    """For tag chains: {ok, reason, address (base form), embedded_tag}. Other chains: ok with no tag."""
    fam = family_for_chain(chain)
    if fam == "xrpl":
        r = parse_xrp_address(address)
        return {"ok": r["ok"], "reason": r["reason"], "address": r["classic"], "embedded_tag": None if r["tag"] is None else str(r["tag"])}
    if fam == "stellar":
        r = parse_stellar_address(address)
        return {"ok": r["ok"], "reason": r["reason"], "address": r["classic"], "embedded_tag": r["tag"]}
    ok, reason = validate_address_for_chain(address, chain)
    return {"ok": ok, "reason": reason, "address": (address or "").strip(), "embedded_tag": None}


def validate_tag_for_chain(tag: str, chain: str) -> tuple[bool, str]:
    fam = family_for_chain(chain)
    if fam == "xrpl":
        return validate_xrp_tag(tag)
    if fam == "stellar":
        return validate_stellar_memo(tag)
    return (False, f"{chain} does not use a memo or tag") if (tag or "").strip() else (True, "ok")


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


def check_destination(address: str, chain: str, destination_type, memo) -> dict:
    """The one rule for a withdrawal destination on any chain.

    Returns {ok, error, message, address, memo, destination_type, embedded_tag,
    needs_tag}. On tag chains (XRP Ledger, Stellar): the customer must have said
    whether the address is self-custody or an exchange; an exchange needs a valid
    memo; a self-custody address must not carry one; an X-address / muxed
    address supplies the tag itself and a conflicting typed memo is refused.
    On other chains a memo or destination type is refused (nothing uses it)."""
    fam = family_for_chain(chain)
    needs_tag = fam in TAG_CHAINS
    memo = (memo or "").strip() or None
    dtype = (destination_type or "").strip().lower() or None
    parsed = parse_tagged_address(address, chain)
    if not parsed["ok"]:
        return {"ok": False, "error": "invalid_wallet_address", "message": parsed["reason"], "needs_tag": needs_tag}
    base = dict(ok=True, error=None, message="ok", address=parsed["address"], embedded_tag=parsed["embedded_tag"],
                needs_tag=needs_tag, destination_type=dtype, memo=memo)
    if not needs_tag:
        if memo:
            return {**base, "ok": False, "error": "memo_not_used", "message": f"{chain} does not use a memo or tag; remove it"}
        if dtype:
            return {**base, "ok": False, "error": "destination_type_not_used", "message": f"{chain} does not ask where the address is; remove destination_type"}
        return base
    word = TAG_CHAINS[fam]
    if dtype not in DESTINATION_TYPES:
        return {**base, "ok": False, "error": "destination_type_required",
                "message": (f"Say where this address is: 'self_custody' (a wallet you control, no {word} needed) or "
                            f"'exchange' (an exchange or custody account, which gives you a {word})")}
    embedded = parsed["embedded_tag"]
    if embedded is not None and memo and memo != embedded:
        return {**base, "ok": False, "error": "memo_conflict",
                "message": f"This address already contains {word} {embedded}; leave the {word} field empty or paste a plain address"}
    if embedded is not None:
        memo = embedded
        if dtype == "self_custody":
            return {**base, "ok": False, "error": "memo_conflict",
                    "message": f"This address embeds {word} {embedded}, which only exchange or custody accounts use; choose 'exchange'"}
    if dtype == "exchange":
        if not memo:
            return {**base, "ok": False, "error": "memo_required",
                    "message": (f"An exchange or custody address on {chain} needs its {word}. Sending without it puts your funds in "
                                f"the exchange's shared account with nothing linking them to you.")}
        ok, reason = validate_tag_for_chain(memo, chain)
        if not ok:
            return {**base, "ok": False, "error": "invalid_memo", "message": reason}
    else:   # self_custody
        if memo:
            return {**base, "ok": False, "error": "memo_not_expected",
                    "message": f"A self-custody wallet never asks you to enter a {word} to receive; remove it or choose 'exchange'"}
    return {**base, "memo": memo, "destination_type": dtype}
