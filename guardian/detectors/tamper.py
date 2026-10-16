from __future__ import annotations

from guardian.detectors.base import Detection


def detect_tamper(consecutive_auth_failures: int, threshold: int) -> list[Detection]:
    """Our own polling key returning auth/permission errors N times in a row
    is a first-class signal, not just an ops error: attackers who compromise
    an account often clean up monitoring keys first. Uses CCXT's own
    AuthenticationError/PermissionDenied exception hierarchy (see
    sentinel/exchange.py TamperSignal) rather than exchange-specific error
    codes, which keeps this reliable without needing Bitget's exact error
    code catalog (an open item in docs/phase0-capability-audit.md section 7)."""
    if consecutive_auth_failures >= threshold:
        return [
            Detection(
                rule="monitoring_key_tamper_suspected",
                severity="HIGH",
                confidence="high",
                message=(
                    f"Guardian's own read-only key failed authentication/permission checks "
                    f"{consecutive_auth_failures} times in a row - it may have been deleted "
                    f"or had its permissions changed"
                ),
                evidence={"consecutive_auth_failures": consecutive_auth_failures, "threshold": threshold},
            )
        ]
    return []
