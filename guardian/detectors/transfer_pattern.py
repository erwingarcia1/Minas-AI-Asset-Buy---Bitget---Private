from __future__ import annotations

from guardian.detectors.base import Detection


def _route_of(transfer: dict) -> str:
    return f"{transfer.get('fromAccount') or '?'}->{transfer.get('toAccount') or '?'}"


def detect_new_transfer_route(new_transfers: list[dict], known_routes: set[str]) -> list[Detection]:
    """Transfers along a from->to route this account has never used before.
    For independent (topology B) accounts these are intra-UID sub-ledger
    moves (spot->futures etc.); for sub-accounts the route includes real
    counterparty user IDs (sub-main-trans-record exposes fromUserId/toUserId
    per the Phase 0 audit). Attackers consolidate funds before withdrawal -
    an unfamiliar route is the earliest visible step of that."""
    detections = []
    fired_routes: set[str] = set()
    for transfer in new_transfers:
        route = _route_of(transfer)
        if route in known_routes or route in fired_routes:
            continue
        fired_routes.add(route)
        detections.append(
            Detection(
                rule="transfer_route_never_seen",
                severity="HIGH",
                confidence="medium",
                message=f"Transfer along a route this account has never used before: {route}",
                evidence={
                    "route": route,
                    "coin": transfer.get("currency"),
                    "amount": transfer.get("amount"),
                    "transfer_id": transfer.get("id"),
                    "timestamp": transfer.get("timestamp"),
                },
            )
        )
    return detections
