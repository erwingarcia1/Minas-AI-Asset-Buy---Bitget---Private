from __future__ import annotations

from guardian.detectors.base import Detection
from guardian.identity import evidence_reference, withdrawal_identity


def detect_unseen_address(new_withdrawal_records: list[dict], known_addresses: set[str]) -> list[Detection]:
    """For each newly-seen withdrawal, flags CRITICAL if its destination
    address has never appeared in this account's deposit or withdrawal
    history. If a record carries no address field at all, emits a degraded
    LOW-confidence detection instead of silently skipping - the exact
    Bitget field name for withdrawal destination address was not confirmed
    against a live key in Phase 0 (docs/phase0-capability-audit.md section
    1), so a missing field is a monitoring gap worth surfacing, not a clean
    pass."""
    detections = []
    for record in new_withdrawal_records:
        address = record.get("address")
        if not address:
            detections.append(
                Detection(
                    rule="unseen_address_check_degraded",
                    severity="LOW",
                    confidence="low (data gap)",
                    message=(
                        "Withdrawal record had no destination address field - "
                        "unseen-address detection could not run for this withdrawal"
                    ),
                    evidence={"order_id": evidence_reference(record, withdrawal_identity(record))},
                )
            )
            continue
        if address not in known_addresses:
            detections.append(
                Detection(
                    rule="withdrawal_to_unseen_address",
                    severity="CRITICAL",
                    confidence="high",
                    message=f"Withdrawal to address never seen before in this account's history: {address}",
                    evidence={
                        "order_id": evidence_reference(record, withdrawal_identity(record)),
                        "address": address,
                        "coin": record.get("currency"),
                        "amount": record.get("amount"),
                    },
                )
            )
    return detections
