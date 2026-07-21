"""honeypot.is v2 client -- transaction SIMULATION, BSC / ETH / Base only.

Docs: https://api.honeypot.is/  (GET /v2/IsHoneypot?address=<addr>&chainID=56)

Response fields we depend on:
  simulationSuccess          bool  -- false means the sim could not run at all
  honeypotResult.isHoneypot  bool
  summary.riskLevel          int   -- 0..100, higher is worse
  simulationResult.buyTax/sellTax  float PERCENT (e.g. 5.0 == 5%), unlike
                                   GoPlus which uses fractions.  Not used for
                                   the tax gate; kept for the log.

A simulator only sees traps that ALREADY fire.  A clean simulation is evidence
about this block, not about the block your buy lands in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

from .errors import HoneypotIsError, MissingFieldError

BASE_URL = "https://api.honeypot.is/v2"
DEFAULT_TIMEOUT = 8.0
BSC_CHAIN_ID = 56

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


@dataclass
class HoneypotReport:
    simulation_success: bool
    is_honeypot: bool
    risk_level: int
    risk: str
    buy_tax_pct: Optional[float]
    sell_tax_pct: Optional[float]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


# ---------- transport ----------

def _request(path: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    try:
        resp = _SESSION.request("GET", f"{BASE_URL}{path}", params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise HoneypotIsError("network", f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code == 429:
        raise HoneypotIsError("rate_limited", "HTTP 429 from honeypot.is", 429)
    if resp.status_code != 200:
        raise HoneypotIsError("http_error", f"HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise HoneypotIsError("bad_json", "response was not JSON", resp.status_code) from exc

    if not isinstance(payload, dict):
        raise HoneypotIsError("bad_json", "response was not a JSON object", resp.status_code)
    if payload.get("error"):
        raise HoneypotIsError("api_error", str(payload["error"]), resp.status_code)
    return payload


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------- public ----------

def fetch(address: str, chain_id: int = BSC_CHAIN_ID,
          timeout: float = DEFAULT_TIMEOUT) -> HoneypotReport:
    """Run the honeypot.is simulation.  Missing required field => raise."""
    addr = (address or "").strip()
    if not addr:
        raise HoneypotIsError("bad_request", "empty address")

    payload = _request("/IsHoneypot", {"address": addr, "chainID": int(chain_id)}, timeout)

    if "simulationSuccess" not in payload:
        raise MissingFieldError("simulationSuccess", "honeypot.is")
    hp = payload.get("honeypotResult")
    if not isinstance(hp, dict) or "isHoneypot" not in hp:
        raise MissingFieldError("honeypotResult.isHoneypot", "honeypot.is")
    summary = payload.get("summary")
    if not isinstance(summary, dict) or summary.get("riskLevel") is None:
        raise MissingFieldError("summary.riskLevel", "honeypot.is")

    risk_level = _num(summary.get("riskLevel"))
    if risk_level is None:
        raise MissingFieldError("summary.riskLevel", "honeypot.is")

    sim = payload.get("simulationResult") or {}
    return HoneypotReport(
        simulation_success=bool(payload["simulationSuccess"]),
        is_honeypot=bool(hp["isHoneypot"]),
        risk_level=int(risk_level),
        risk=str(summary.get("risk", "")),
        buy_tax_pct=_num(sim.get("buyTax")),
        sell_tax_pct=_num(sim.get("sellTax")),
        raw=payload,
    )


__all__ = ["fetch", "HoneypotReport", "BASE_URL", "DEFAULT_TIMEOUT"]
