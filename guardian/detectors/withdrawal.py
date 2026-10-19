from __future__ import annotations

from guardian.detectors.base import Detection
from guardian.identity import evidence_reference, withdrawal_identity


def detect_new_withdrawals(new_withdrawal_records: list[dict]) -> list[Detection]:
    """new_withdrawal_records must already be filtered down to withdrawals
    NOT previously seen (see baseline.record_seen_withdrawal). Fires
    CRITICAL on first sighting regardless of status - pending/in-flight
    withdrawals are alerted on, not just completed ones. Bitget's withdrawal
    status enum (pending, pending_review, pending_fail, pending_review_fail,
    reject, success) is a real in-flight state machine, confirmed in
    docs/phase0-capability-audit.md section 1.

    `order_id` in the evidence is the exchange's own reference where there is
    one, and the derived identity where there isn't. That field is not
    cosmetic: `dispatch._dedup_key` falls back to the *coin* when it is empty,
    so leaving it None makes two unrelated unreferenced withdrawals of the same
    coin a single alert and suppresses the second for a full cooldown (G-11)."""
    detections = []
    for record in new_withdrawal_records:
        detections.append(
            Detection(
                rule="withdrawal_initiated",
                severity="CRITICAL",
                confidence="high",
                message=(
                    f"Withdrawal of {record.get('amount')} {record.get('currency')} "
                    f"initiated, status={record.get('status')}"
                ),
                evidence={
                    "order_id": evidence_reference(record, withdrawal_identity(record)),
                    "coin": record.get("currency"),
                    "amount": record.get("amount"),
                    "status": record.get("status"),
                    "address": record.get("address"),
                    "timestamp": record.get("timestamp"),
                },
            )
        )
    return detections
