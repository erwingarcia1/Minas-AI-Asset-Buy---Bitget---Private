"""Pure guardrail predicates for the meme-buy subsystem.

Every function in this module is a pure predicate: no network, no database, no
mutation of anything.  The single exception is ``kill_switch_engaged`` which
does one ``os.path.exists`` — deliberately the cheapest possible check so the
operator can halt the pipeline with ``touch /path/to/STOP`` and no password
ceremony, from any shell, in under a second.

Address format references (the only external "API" this module encodes):
  EVM / BSC 20-byte hex address:
    https://ethereum.org/en/developers/docs/accounts/
  Solana base58 (Bitcoin alphabet, no 0 O I l) ed25519 pubkey:
    https://solana.com/docs/core/accounts

Design rule: FAIL CLOSED.  Every function returns *deny* when its inputs are
absent, malformed, or of an unexpected type.  Absent data is never safe data.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

# ---------- constants ----------

CHAIN_BSC = "bsc"
CHAIN_SOLANA = "solana"
SUPPORTED_CHAINS = (CHAIN_BSC, CHAIN_SOLANA)

# 25% is already an absurd slippage for a deliberate buy; anything above it is
# assumed to be a typo (e.g. "5000" meaning 5.000%) and rejected at config time.
MAX_SLIPPAGE_BPS = 2500

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass(frozen=True)
class CapResult:
    """Result of a guardrail check.  Falsy when the check denies."""

    ok: bool
    reason: Optional[str] = None

    def __bool__(self) -> bool:  # so `if not check_...(): abort`
        return self.ok


_OK = CapResult(True)


def _deny(reason: str) -> CapResult:
    return CapResult(False, reason)


def _as_amount(value: Any) -> Optional[float]:
    """Coerce to a finite float, or None.  bool is rejected (bool is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---------- public: address helpers ----------


def is_valid_address(chain: str, address: Any) -> bool:
    """True only for a well-formed address on a *supported* chain."""
    if not isinstance(address, str) or not isinstance(chain, str):
        return False
    chain = chain.strip().lower()
    address = address.strip()
    if chain == CHAIN_BSC:
        return bool(_EVM_ADDRESS_RE.match(address))
    if chain == CHAIN_SOLANA:
        return bool(_SOLANA_ADDRESS_RE.match(address))
    return False  # unknown chain -> not valid, never "probably fine"


def normalize_address(chain: str, address: Any) -> Optional[str]:
    """Canonical form for comparison: EVM lowercased, Solana case-sensitive."""
    if not is_valid_address(chain, address):
        return None
    address = address.strip()
    return address.lower() if chain.strip().lower() == CHAIN_BSC else address


# ---------- public: caps ----------


def check_per_trade_cap(amount: Any, cap: Any) -> CapResult:
    """Deny unless 0 < amount <= cap, with both finite numbers."""
    a = _as_amount(amount)
    c = _as_amount(cap)
    if a is None:
        return _deny("per_trade: amount is not a finite number")
    if c is None or c <= 0:
        return _deny("per_trade: cap is not a positive finite number")
    if a <= 0:
        return _deny("per_trade: amount must be > 0")
    if a > c:
        return _deny("per_trade: amount %.8g exceeds cap %.8g" % (a, c))
    return _OK


def check_daily_cap(amount: Any, spent_24h: Any, cap: Any) -> CapResult:
    """Deny unless spent_24h + amount <= cap (rolling 24h window)."""
    a = _as_amount(amount)
    s = _as_amount(spent_24h)
    c = _as_amount(cap)
    if a is None or a <= 0:
        return _deny("daily: amount must be a positive finite number")
    if s is None or s < 0:
        return _deny("daily: spent_24h is unknown or negative")
    if c is None or c <= 0:
        return _deny("daily: cap is not a positive finite number")
    if s + a > c:
        return _deny(
            "daily: %.8g spent + %.8g requested exceeds cap %.8g" % (s, a, c)
        )
    return _OK


# ---------- public: allowlists ----------


def check_address_allowlist(address: Any, allowlist: Any) -> CapResult:
    """Deny unless `address` is literally present in `allowlist`.

    Withdrawal destinations are config-only.  This is the code-level assertion
    that the value actually handed to the exchange came from that config and
    was not derived at runtime.  An empty allowlist always denies.
    """
    if not isinstance(address, str) or not address.strip():
        return _deny("address allowlist: destination address is missing")
    if isinstance(allowlist, (str, bytes)) or not isinstance(allowlist, Iterable):
        return _deny("address allowlist: allowlist is not a list")
    entries = [e for e in allowlist if isinstance(e, str) and e.strip()]
    if not entries:
        return _deny("address allowlist: allowlist is empty")
    want = address.strip()
    for entry in entries:
        e = entry.strip()
        if e == want:
            return _OK
        # EVM addresses are case-insensitive (EIP-55 checksum is cosmetic).
        if _EVM_ADDRESS_RE.match(e) and _EVM_ADDRESS_RE.match(want):
            if e.lower() == want.lower():
                return _OK
    return _deny("address allowlist: %s is not an allowlisted destination" % want)


def check_token_allowlist(chain: Any, token: Any, allowlist: Any) -> CapResult:
    """Deny unless `token` is allowlisted *for that chain*.

    `allowlist` may be a mapping chain -> iterable of addresses, or a flat
    iterable already scoped to the chain.
    """
    if not isinstance(chain, str) or chain.strip().lower() not in SUPPORTED_CHAINS:
        return _deny("token allowlist: unsupported chain %r" % (chain,))
    chain = chain.strip().lower()
    want = normalize_address(chain, token)
    if want is None:
        return _deny("token allowlist: %r is not a valid %s address" % (token, chain))

    if isinstance(allowlist, Mapping):
        entries = allowlist.get(chain)
    else:
        entries = allowlist
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
        return _deny("token allowlist: no allowlist configured for %s" % chain)
    normalized = {normalize_address(chain, e) for e in entries}
    normalized.discard(None)
    if not normalized:
        return _deny("token allowlist: allowlist for %s is empty" % chain)
    if want not in normalized:
        return _deny("token allowlist: %s is not allowlisted on %s" % (want, chain))
    return _OK


# ---------- public: kill switch ----------


def kill_switch_engaged(path: Any) -> bool:
    """True (= STOP) if the kill-switch file exists.

    Returns True — engaged — when `path` is unset or the filesystem cannot be
    read.  An unconfigured or unreadable kill switch means the operator has no
    way to halt us, which is itself a reason not to run.
    """
    if not path or not isinstance(path, (str, bytes, os.PathLike)):
        return True
    try:
        return os.path.exists(path)
    except OSError:
        return True
