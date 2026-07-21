"""Token safety gate -- the fail-closed pre-trade screen.

    from safety import screen_token
    verdict = screen_token("bsc", token_address, price_impact_bps=quote.impact_bps)
    if not verdict.passed:
        log.warning("token rejected: %s", verdict.reasons)
        return                      # NEVER spend on a non-passing verdict

The gate is read-only (GET/JSON-RPC only) and cannot raise: every exception,
timeout, HTTP 429 and missing field becomes a failed check.  Read the
LIMITATIONS section of ``safety.gate``'s docstring before trusting a pass --
in particular, nothing here scores whether a perfectly sellable token is
worth anything.
"""

from .errors import (AddressFormatError, AllowlistError, DexScreenerError,
                     GoPlusError, HoneypotIsError, MissingFieldError,
                     RugCheckError, SafetyProviderError, SolanaRPCError)
from .gate import (ALLOWLIST_ENV, CheckResult, GateVerdict, Providers,
                   SUPPORTED_CHAINS, Thresholds, default_providers,
                   load_allowlist, screen_token)

__all__ = [
    "screen_token", "GateVerdict", "CheckResult", "Thresholds", "Providers",
    "default_providers", "load_allowlist", "ALLOWLIST_ENV", "SUPPORTED_CHAINS",
    "SafetyProviderError", "GoPlusError", "HoneypotIsError", "RugCheckError",
    "DexScreenerError", "SolanaRPCError", "MissingFieldError", "AllowlistError",
    "AddressFormatError",
]
