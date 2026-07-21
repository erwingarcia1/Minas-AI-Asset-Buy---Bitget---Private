"""RugCheck (Solana) client.

Docs: https://api.rugcheck.xyz/swagger/index.html
  GET /v1/tokens/{mint}/report/summary  -> risks[], score, score_normalised
  GET /v1/tokens/{mint}/report          -> markets[].lp.lpLockedPct, lockers{}

DIRECTION WARNING -- ``score_normalised`` is HIGHER = WORSE.  Verified live:
USDC scored 1, BONK 7, a token with a live mint authority 76.  Inverting this
comparison turns the gate into a scam detector that only buys scams, so the
constant, this comment and a dedicated unit test all pin the direction.

Lockers are parsed for their UNLOCK DATE, not just the locked percentage: "100%
locked" that unlocks in 20 minutes is not locked.  An unlock date of 0 / absent
is treated as UNVERIFIABLE and rejected -- deliberately conservative, it will
also reject some genuinely burned LP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .errors import MissingFieldError, RugCheckError

BASE_URL = "https://api.rugcheck.xyz/v1"
DEFAULT_TIMEOUT = 8.0

# Higher score_normalised == riskier token.  Do not flip.
SCORE_HIGHER_IS_WORSE = True

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


@dataclass
class Locker:
    owner: str
    unlock_date: Optional[int]      # unix seconds; None/0 == unverifiable
    usdc_locked: Optional[float]
    kind: str


@dataclass
class RugCheckReport:
    score_normalised: float
    risks: List[Dict[str, Any]]
    danger_risks: List[str]
    lp_locked_pct: Optional[float]  # 0..100
    lockers: List[Locker]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


# ---------- transport ----------

def _request(path: str, timeout: float) -> Any:
    try:
        resp = _SESSION.request("GET", f"{BASE_URL}{path}", timeout=timeout)
    except requests.RequestException as exc:
        raise RugCheckError("network", f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code == 429:
        raise RugCheckError("rate_limited", "HTTP 429 from RugCheck", 429)
    if resp.status_code != 200:
        raise RugCheckError("http_error", f"HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise RugCheckError("bad_json", "response was not JSON", resp.status_code) from exc
    if payload is None:
        raise RugCheckError("no_data", "RugCheck returned null")
    return payload


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pool_usd(lp: Dict[str, Any]) -> float:
    """How BIG the pool is -- deliberately not how much of it is locked."""
    base, quote = _num(lp.get("baseUSD")), _num(lp.get("quoteUSD"))
    if base is not None or quote is not None:
        return (base or 0.0) + (quote or 0.0)
    return _num(lp.get("lpLockedUSD")) or 0.0


def _primary_market(report: Dict[str, Any]) -> Dict[str, Any]:
    """The pool that actually matters: the DEEPEST one.

    Ranking by ``lpLockedUSD`` (what this did before) let a token choose which
    pool got inspected by fully locking a decoy: the metric that selects the
    pool must never be the metric the pool is then judged on.  Depth also
    matches how ``dexscreener.fetch`` picks its pair, so both halves of the
    gate are looking at the same pool.
    """
    markets = report.get("markets")
    if not isinstance(markets, list) or not markets:
        raise MissingFieldError("markets", "rugcheck")
    best, best_usd = None, -1.0
    for market in markets:
        lp = (market or {}).get("lp") or {}
        usd = _pool_usd(lp)
        if usd > best_usd:
            best, best_usd = market, usd
    return best or {}


# ---------- public ----------

def fetch(mint: str, timeout: float = DEFAULT_TIMEOUT) -> RugCheckReport:
    """Summary (score + risks) plus the full report (LP lock + lockers)."""
    addr = (mint or "").strip()
    if not addr:
        raise RugCheckError("bad_request", "empty mint")

    summary = _request(f"/tokens/{addr}/report/summary", timeout)
    if not isinstance(summary, dict):
        raise RugCheckError("bad_json", "summary was not a JSON object")
    if summary.get("score_normalised") is None:
        raise MissingFieldError("score_normalised", "rugcheck")
    score = _num(summary["score_normalised"])
    if score is None:
        raise MissingFieldError("score_normalised", "rugcheck")

    risks = summary.get("risks")
    if not isinstance(risks, list):
        raise MissingFieldError("risks", "rugcheck")
    danger = [str(r.get("name", "unnamed")) for r in risks
              if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger"]

    report = _request(f"/tokens/{addr}/report", timeout)
    if not isinstance(report, dict):
        raise RugCheckError("bad_json", "report was not a JSON object")

    lp = _primary_market(report).get("lp") or {}
    lp_locked_pct = _num(lp.get("lpLockedPct"))

    lockers: List[Locker] = []
    raw_lockers = report.get("lockers")
    if isinstance(raw_lockers, dict):
        raw_lockers = list(raw_lockers.values())
    for entry in raw_lockers or []:
        if not isinstance(entry, dict):
            continue
        unlock = _num(entry.get("unlockDate"))
        lockers.append(Locker(
            owner=str(entry.get("owner", "")),
            unlock_date=int(unlock) if unlock else None,
            usdc_locked=_num(entry.get("usdcLocked")),
            kind=str(entry.get("type", "")),
        ))

    return RugCheckReport(
        score_normalised=score,
        risks=risks,
        danger_risks=danger,
        lp_locked_pct=lp_locked_pct,
        lockers=lockers,
        raw={"summary": summary, "report": report},
    )


__all__ = ["fetch", "RugCheckReport", "Locker", "SCORE_HIGHER_IS_WORSE",
           "BASE_URL", "DEFAULT_TIMEOUT"]
