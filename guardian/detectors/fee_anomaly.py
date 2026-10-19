from __future__ import annotations

from decimal import Decimal

from guardian.detectors.base import Detection
from guardian.numeric import to_decimal


def sum_fees_by_coin(fills: list[dict]) -> dict[str, Decimal]:
    fees: dict[str, Decimal] = {}
    for fill in fills:
        fee = fill.get("fee") or {}
        coin = fee.get("currency")
        cost = to_decimal(fee.get("cost"))
        if coin and cost > 0:
            fees[coin] = fees.get(coin, Decimal("0")) + cost
    return fees


def update_ema(previous_ema: Decimal | None, observation: Decimal, alpha: Decimal) -> Decimal:
    if previous_ema is None:
        return observation
    return alpha * observation + (Decimal("1") - alpha) * previous_ema


def detect_fee_anomaly(
    window_fees: dict[str, Decimal],
    baseline_ema: dict[str, Decimal],
    multiplier: Decimal,
    min_fee_floor: Decimal,
) -> list[Detection]:
    """Fee spend in this window vs the account's own EMA baseline, per fee
    coin. An attacker churning the account through wash trades (to drain via
    spreads/fees or manufacture volume) shows up here even when each
    individual trade looks unremarkable. min_fee_floor suppresses noise on
    accounts whose baseline is near zero - a jump from 0.001 to 0.01 USDT is
    ten-fold but meaningless."""
    detections = []
    for coin, spent in window_fees.items():
        if spent < min_fee_floor:
            continue
        baseline = baseline_ema.get(coin)
        if baseline is None or baseline <= 0:
            continue  # first observation seeds the baseline instead of alerting
        if spent > baseline * multiplier:
            detections.append(
                Detection(
                    rule="abnormal_fee_spend",
                    severity="MEDIUM",
                    confidence="medium",
                    message=(
                        f"Fee spend of {spent} {coin} this window is more than {multiplier}x "
                        f"the account's baseline ({baseline} {coin})"
                    ),
                    evidence={
                        "coin": coin,
                        "window_fee_spend": str(spent),
                        "baseline_ema": str(baseline),
                        "multiplier_threshold": str(multiplier),
                    },
                )
            )
    return detections
