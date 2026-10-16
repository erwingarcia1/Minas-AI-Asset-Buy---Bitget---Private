from __future__ import annotations

from guardian.detectors.base import Detection


def detect_new_pair_activity(fills: list[dict], known_symbols: set[str]) -> list[Detection]:
    """Trading activity in a pair this account has never touched. Severity is
    HIGH because counterparty-drain attacks route stolen value through
    illiquid pairs the victim never traded. Accounts tagged as active
    traders get a wider baseline seeded at onboarding, so strategy rotation
    doesn't false-positive (see docs/phase0-capability-audit.md section 6)."""
    detections = []
    fired: set[str] = set()
    for fill in fills:
        symbol = fill.get("symbol")
        if not symbol or symbol in known_symbols or symbol in fired:
            continue
        fired.add(symbol)
        detections.append(
            Detection(
                rule="never_touched_pair_activity",
                severity="HIGH",
                confidence="medium",
                message=f"Trading activity in {symbol}, a pair this account has never traded before",
                evidence={
                    "symbol": symbol,
                    "side": fill.get("side"),
                    "amount": fill.get("filled") or fill.get("amount"),
                    "timestamp": fill.get("timestamp"),
                },
            )
        )
    return detections
