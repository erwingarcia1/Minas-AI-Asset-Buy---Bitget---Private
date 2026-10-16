from __future__ import annotations

from guardian.detectors.base import Detection


def detect_permission_state_change(previous: dict | None, current: dict) -> list[Detection]:
    """Bitget exposes no endpoint for an independent account to list its own
    API keys (confirmed absent, docs/phase0-capability-audit.md section 1).
    This is the documented proxy for the 'new API key created' catalog item:
    watch OUR OWN key's authorities/ip-whitelist for drift. It cannot detect
    a new sibling key being created directly - only changes visible through
    our own key's account-info response - so it is always emitted at reduced
    confidence and explicitly labeled as a proxy so alert recipients don't
    mistake it for direct key-inventory visibility. (Topology A sub-accounts
    have a real direct signal instead - see the Phase 0 audit - this proxy
    is the topology-B fallback.)"""
    if previous is None:
        return []
    if previous.get("authorities") == current.get("authorities") and previous.get(
        "ip_whitelist"
    ) == current.get("ip_whitelist"):
        return []
    return [
        Detection(
            rule="account_permission_state_changed",
            severity="HIGH",
            confidence="low (proxy - Bitget exposes no API-key inventory for independent accounts)",
            message=(
                "Account permission/IP-whitelist state changed since last check. "
                "This cannot confirm a new API key was created - Bitget doesn't expose "
                "that for independent accounts - but it is the closest available signal "
                "and warrants manual review."
            ),
            evidence={"previous": previous, "current": current},
        )
    ]
