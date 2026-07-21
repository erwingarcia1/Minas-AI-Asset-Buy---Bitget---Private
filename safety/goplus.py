"""GoPlus Security token-security client (no API key, ~30 req/min).

Docs: https://docs.gopluslabs.io/reference/token-security-api
EVM   : GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses=<addr>
        -> result keyed by the LOWERCASED contract address, flags are "0"/"1"
           strings, taxes are FRACTION strings ("0.05" == 5%).
Solana: GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=<mint>
        -> a COMPLETELY DIFFERENT schema (nested {authority, status} objects,
           different field names).  Two independent parsers on purpose; sharing
           one would silently read a missing key as a safe value.

CRITICAL: GoPlus OMITS fields it cannot determine.  Verified live -- PancakeSwap's
own CAKE returned no cannot_sell_all, no buy_tax, no sell_tax, no lp_holders.
These functions therefore return the RAW dict; presence assertion is the gate's
job and absence is always a REJECT.
"""

from __future__ import annotations

from typing import Any, Dict

import requests

from .errors import GoPlusError

BASE_URL = "https://api.gopluslabs.io/api/v1"
DEFAULT_TIMEOUT = 8.0
BSC_CHAIN_ID = 56

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


# ---------- transport ----------

def _request(path: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """GET a GoPlus endpoint and return ``result``.

    Raises GoPlusError on transport failure, non-200, HTTP 429 or any business
    code other than 1.  Code 2 ("data not ready / partial") is a REJECT: partial
    security data is exactly the case where a scam token looks clean.
    No retries -- a retry that eventually succeeds would mask a rate limit.
    """
    try:
        resp = _SESSION.request("GET", f"{BASE_URL}{path}", params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise GoPlusError("network", f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code == 429:
        raise GoPlusError("rate_limited", "HTTP 429 from GoPlus", 429)
    if resp.status_code != 200:
        raise GoPlusError("http_error", f"HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise GoPlusError("bad_json", "response was not JSON", resp.status_code) from exc

    if not isinstance(payload, dict):
        raise GoPlusError("bad_json", "response was not a JSON object", resp.status_code)

    code = payload.get("code")
    if code != 1:
        raise GoPlusError(code, str(payload.get("message", "unexpected GoPlus code")),
                          resp.status_code)

    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        raise GoPlusError("no_data", "GoPlus returned an empty result", resp.status_code)
    return result


def _pick(result: Dict[str, Any], key: str, case_insensitive: bool = False) -> Dict[str, Any]:
    """Look up the per-token entry.

    ``case_insensitive`` is for EVM ONLY, where hex addresses are the same
    address in any casing.  Base58 is CASE-SENSITIVE: two different Solana
    mints can differ only in case, so folding case there could hand back a
    different token's security record and screen the wrong mint.
    """
    entry = result.get(key)
    if entry is None and case_insensitive:
        lowered = key.lower()
        for addr, value in result.items():
            if str(addr).lower() == lowered:
                entry = value
                break
    if not isinstance(entry, dict) or not entry:
        raise GoPlusError("no_data", f"GoPlus has no security data for {key}")
    return entry


# ---------- public ----------

def fetch_evm(address: str, chain_id: int = BSC_CHAIN_ID,
              timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Raw EVM (BSC) token-security dict.  Keys may be ABSENT -- assert presence."""
    addr = (address or "").strip()
    if not addr:
        raise GoPlusError("bad_request", "empty address")
    result = _request(f"/token_security/{int(chain_id)}",
                      {"contract_addresses": addr.lower()}, timeout)
    return _pick(result, addr.lower(), case_insensitive=True)


def fetch_solana(mint: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Raw Solana token-security dict -- different schema from fetch_evm()."""
    addr = (mint or "").strip()
    if not addr:
        raise GoPlusError("bad_request", "empty mint")
    result = _request("/solana/token_security", {"contract_addresses": addr}, timeout)
    return _pick(result, addr)


# ---------- shared parsing helpers ----------
# Kept here (next to the schema notes) but deliberately dumb: they raise rather
# than defaulting, so a missing key can never be read as a benign value.

def solana_authority_status(entry: Dict[str, Any], field: str) -> str:
    """Read a Solana ``{"authority": [...], "status": "0"}`` sub-object's status.

    Returns the raw "0"/"1" string; raises KeyError-free GoPlusError if absent
    so the caller converts it into a missing-field reject.
    """
    node = entry.get(field)
    if isinstance(node, dict) and node.get("status") not in (None, ""):
        return str(node["status"])
    if isinstance(node, str) and node != "":
        return node
    raise GoPlusError("missing_field", f"field {field} missing")


__all__ = ["fetch_evm", "fetch_solana", "solana_authority_status",
           "BASE_URL", "BSC_CHAIN_ID", "DEFAULT_TIMEOUT"]
