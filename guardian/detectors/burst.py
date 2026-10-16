from __future__ import annotations

from datetime import datetime, timedelta

from guardian.detectors.base import Detection


def detect_withdrawal_burst(
    withdrawal_times: list[datetime],
    window_minutes: int,
    min_count: int,
    now: datetime,
) -> list[Detection]:
    """Structuring detection: several withdrawals inside a short window is a
    classic pattern for staying under per-withdrawal alert thresholds. Fires
    once per window on the count, independent of sizes - the per-withdrawal
    CRITICAL rule already covers each individual event; this adds the
    aggregate signal."""
    window_start = now - timedelta(minutes=window_minutes)
    in_window = [t for t in withdrawal_times if t >= window_start]
    if len(in_window) >= min_count:
        return [
            Detection(
                rule="withdrawal_burst",
                severity="HIGH",
                confidence="high",
                message=(
                    f"{len(in_window)} withdrawals within {window_minutes} minutes - "
                    f"possible structuring to stay under alert thresholds"
                ),
                evidence={
                    "count_in_window": len(in_window),
                    "window_minutes": window_minutes,
                    "threshold": min_count,
                    "timestamps": [t.isoformat() for t in in_window],
                },
            )
        ]
    return []
