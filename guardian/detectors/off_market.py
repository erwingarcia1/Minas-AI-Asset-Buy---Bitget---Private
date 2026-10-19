from __future__ import annotations

from decimal import Decimal

from guardian.detectors.base import Detection
from guardian.numeric import to_decimal


def detect_off_market_fills(
    fills: list[dict],
    reference_prices: dict[str, Decimal],
    max_deviation_pct: Decimal,
) -> list[Detection]:
    """Fills executed far from the market reference price - the classic
    counterparty-drain: an attacker with a trade-scope key sells the
    victim's assets into their own resting orders on an illiquid book at an
    absurd price. reference_prices comes from the public ticker for spot and
    mark/index price for futures (spot has no mark-price concept - Phase 0
    audit, trading-drain section). Fills whose symbol has no reference price
    are skipped silently: no reference means no basis for a deviation claim."""
    detections = []
    for fill in fills:
        symbol = fill.get("symbol")
        reference = reference_prices.get(symbol) if symbol else None
        if not reference or reference <= 0:
            continue
        price = to_decimal(fill.get("average") or fill.get("price"))
        if price <= 0:
            continue
        deviation_pct = abs(price - reference) / reference * Decimal("100")
        if deviation_pct > max_deviation_pct:
            detections.append(
                Detection(
                    rule="off_market_execution",
                    severity="HIGH",
                    confidence="high",
                    message=(
                        f"Fill on {symbol} executed {deviation_pct.quantize(Decimal('0.01'))}% away "
                        f"from reference price - possible counterparty-drain trade"
                    ),
                    evidence={
                        "symbol": symbol,
                        "fill_price": str(price),
                        "reference_price": str(reference),
                        "deviation_pct": str(deviation_pct.quantize(Decimal("0.01"))),
                        "side": fill.get("side"),
                        "amount": fill.get("filled") or fill.get("amount"),
                        "order_id": fill.get("id"),
                    },
                )
            )
    return detections
