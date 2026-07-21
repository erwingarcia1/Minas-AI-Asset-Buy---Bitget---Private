"""Typed errors for the token safety gate.

Every provider module owns one error class.  They all subclass the repo-wide
``exchange_errors.ExchangeAPIError`` so a caller can catch one type across the
exchange clients and the on-chain screeners alike.

The gate converts *any* of these into a FAILED check -- never into a pass.
"""

from __future__ import annotations

try:  # pragma: no cover - trivial import shim
    from exchange_errors import ExchangeAPIError
except ImportError:  # pragma: no cover
    # The repo is flat and ``exchange_errors.py`` sits at its root.  When the
    # safety package is imported on its own (CI job that only ships safety/),
    # fall back to an identical shim so imports never fail closed-open.
    class ExchangeAPIError(Exception):
        """Mirror of exchange_errors.ExchangeAPIError."""

        exchange = "unknown"

        def __init__(self, code, message, http_status=None):
            super().__init__(message)
            self.code = code
            self.message = message
            self.http_status = http_status

        def __str__(self):
            return f"{self.exchange} error {self.code}: {self.message}"


# ---------- provider errors ----------

class SafetyProviderError(ExchangeAPIError):
    """Base class for every safety-data provider failure."""

    exchange = "safety"


class GoPlusError(SafetyProviderError):
    exchange = "goplus"


class HoneypotIsError(SafetyProviderError):
    exchange = "honeypot.is"


class RugCheckError(SafetyProviderError):
    exchange = "rugcheck"


class DexScreenerError(SafetyProviderError):
    exchange = "dexscreener"


class SolanaRPCError(SafetyProviderError):
    exchange = "solana-rpc"


# ---------- gate errors ----------

class MissingFieldError(SafetyProviderError):
    """A field the gate REQUIRES was absent from a provider response.

    GoPlus silently omits fields it could not determine -- verified live, even
    PancakeSwap's own CAKE came back with no ``buy_tax``/``sell_tax``/
    ``lp_holders``.  Absent data is never "safe" data: it is a reject.
    """

    exchange = "safety"

    def __init__(self, field: str, source: str = "provider"):
        super().__init__("missing_field", f"field {field} missing", None)
        self.field = field
        self.source = source


class AllowlistError(SafetyProviderError):
    """The configured token allowlist could not be loaded or does not match."""

    exchange = "safety"


class AddressFormatError(SafetyProviderError):
    """Address failed the syntactic / checksum validation for its chain."""

    exchange = "safety"


__all__ = [
    "ExchangeAPIError",
    "SafetyProviderError",
    "GoPlusError",
    "HoneypotIsError",
    "RugCheckError",
    "DexScreenerError",
    "SolanaRPCError",
    "MissingFieldError",
    "AllowlistError",
    "AddressFormatError",
]
