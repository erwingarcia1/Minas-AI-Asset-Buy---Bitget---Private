"""DexScreener market-data client.

Docs: https://docs.dexscreener.com/api/reference
  GET https://api.dexscreener.com/latest/dex/tokens/{address}

Rate limits: 60 req/min is documented for the token-profile endpoints.  The
widely repeated "300/min" for /latest/dex/* is UNVERIFIED, so nothing here
hardcodes it -- we honour HTTP 429 (and Retry-After, for the caller's log) and
treat it as a REJECT instead of retrying into a pass.

Fields consumed: liquidity.usd, pairCreatedAt (ms), volume.h24,
txns.m5.{buys,sells}.  Any of them absent => the gate rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .errors import DexScreenerError

BASE_URL = "https://api.dexscreener.com"
DEFAULT_TIMEOUT = 6.0

# DexScreener chainId strings for the chains this bot trades.
CHAIN_IDS = {"bsc": "bsc", "solana": "solana"}

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


@dataclass
class PairSnapshot:
    chain_id: str
    pair_address: str
    dex_id: str
    liquidity_usd: Optional[float]
    pair_created_at_ms: Optional[int]
    volume_h24_usd: Optional[float]
    txns_m5_buys: Optional[int]
    txns_m5_sells: Optional[int]
    price_usd: Optional[float]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


# ---------- transport ----------

def _request(path: str, timeout: float) -> Dict[str, Any]:
    try:
        resp = _SESSION.request("GET", f"{BASE_URL}{path}", timeout=timeout)
    except requests.RequestException as exc:
        raise DexScreenerError("network", f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "?") if hasattr(resp, "headers") else "?"
        raise DexScreenerError("rate_limited",
                               f"HTTP 429 from DexScreener (Retry-After={retry_after})", 429)
    if resp.status_code != 200:
        raise DexScreenerError("http_error", f"HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise DexScreenerError("bad_json", "response was not JSON", resp.status_code) from exc
    if not isinstance(payload, dict):
        raise DexScreenerError("bad_json", "response was not a JSON object", resp.status_code)
    return payload


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    num = _num(value)
    return int(num) if num is not None else None


# ---------- public ----------

def fetch(address: str, chain: str, timeout: float = DEFAULT_TIMEOUT) -> PairSnapshot:
    """Deepest pool for ``address`` on ``chain`` (bsc|solana).

    "Deepest" = highest liquidity.usd, because that is the pool a router will
    actually route through and therefore the one whose depth constrains the
    exit.  A pair with no liquidity figure sorts last -- it is never "deepest".
    """
    addr = (address or "").strip()
    chain_id = CHAIN_IDS.get((chain or "").strip().lower())
    if not addr:
        raise DexScreenerError("bad_request", "empty address")
    if not chain_id:
        raise DexScreenerError("bad_request", f"unsupported chain {chain!r}")

    payload = _request(f"/latest/dex/tokens/{addr}", timeout)
    pairs: List[Dict[str, Any]] = [p for p in (payload.get("pairs") or [])
                                   if isinstance(p, dict) and p.get("chainId") == chain_id]
    if not pairs:
        raise DexScreenerError("no_data", f"no {chain_id} pairs for {addr}")

    best = max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")) or -1.0)
    txns_m5 = (best.get("txns") or {}).get("m5") or {}

    return PairSnapshot(
        chain_id=chain_id,
        pair_address=str(best.get("pairAddress", "")),
        dex_id=str(best.get("dexId", "")),
        liquidity_usd=_num((best.get("liquidity") or {}).get("usd")),
        pair_created_at_ms=_int(best.get("pairCreatedAt")),
        volume_h24_usd=_num((best.get("volume") or {}).get("h24")),
        txns_m5_buys=_int(txns_m5.get("buys")),
        txns_m5_sells=_int(txns_m5.get("sells")),
        price_usd=_num(best.get("priceUsd")),
        raw=best,
    )


__all__ = ["fetch", "PairSnapshot", "CHAIN_IDS", "BASE_URL", "DEFAULT_TIMEOUT"]
