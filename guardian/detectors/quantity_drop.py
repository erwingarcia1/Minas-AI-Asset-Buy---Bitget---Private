from __future__ import annotations

import logging
from decimal import Decimal

from guardian.detectors.base import Detection
from guardian.numeric import to_decimal

logger = logging.getLogger(__name__)

# How far past the window start a detection would still have been suppressed by
# a wider settlement grace. Fires a "near miss" log line so the grace can be
# tuned from evidence instead of guesswork - see G-7.
NEAR_MISS_PROBE_SECONDS = 300


def _in_window(
    record: dict, window_start_ms: int | None, source: str, exchange: str, metrics=None,
    account_label: str | None = None,
) -> bool:
    """True if this record can be proven to fall at or after the window start.

    A record with no usable timestamp CANNOT be proven to be in-window, so it
    is excluded - it must not explain away a drop. Same principle as the
    read-only key check: when the system can't tell, it refuses to assume
    safe. Each exclusion is logged AND counted on
    `sentinel_untimestamped_records_total`, because nobody reads logs on a
    quiet day: a Binance response-format change has to reach an operator
    through an alarm, before it reaches a customer as a missed alert.

    `exchange` and `account_label` answer different questions and must not be
    conflated (#55). The metric is labelled by exchange, because an alarm on it
    needs to name the integration that changed, and because its cardinality has
    to stay bounded by how many exchanges we support rather than by how many
    customers we sign. The log line names the account, because that is what an
    operator reading it wants to know."""
    if window_start_ms is None:
        return True

    raw = record.get("timestamp")
    if raw is None:
        logger.warning(
            "%s: %s %s has no timestamp - not counting it as an explanation for a balance drop",
            account_label or exchange,
            source,
            record.get("id") or record.get("symbol") or "<unidentified>",
        )
        if metrics is not None:
            metrics.untimestamped_record(exchange, source)
        return False

    try:
        return int(raw) >= window_start_ms
    except (TypeError, ValueError):
        logger.warning(
            "%s: %s %s has an unparseable timestamp %r - not counting it as an explanation",
            account_label or exchange,
            source,
            record.get("id") or record.get("symbol") or "<unidentified>",
            raw,
        )
        if metrics is not None:
            metrics.untimestamped_record(exchange, source)
        return False


def _explained_outflow_for_coin(coin: str, withdrawals: list[dict], fills: list[dict]) -> Decimal:
    outflow = Decimal("0")

    for w in withdrawals:
        if w.get("currency") != coin:
            continue
        outflow += to_decimal(w.get("amount"))
        fee = w.get("fee") or {}
        if fee.get("currency") == coin:
            outflow += to_decimal(fee.get("cost"))

    for f in fills:
        symbol = f.get("symbol") or ""
        base, _, quote = symbol.partition("/")
        side = f.get("side")
        # `fills` are CCXT unified Order records (from fetch_closed_orders,
        # which - unlike fetch_my_trades - doesn't require a per-call symbol
        # on Bitget). Use the EXECUTED quantity (`filled`), not the requested
        # order `amount`, which can differ on a partial fill. Trade-shaped
        # records (tests, or a future per-symbol trades pull) fall back to
        # `amount`/`cost` since they have no `filled` field.
        executed_qty = f.get("filled") if f.get("filled") is not None else f.get("amount")
        if side == "sell" and base == coin:
            outflow += to_decimal(executed_qty)
        elif side == "buy" and quote == coin:
            outflow += to_decimal(f.get("cost"))
        fee = f.get("fee") or {}
        if fee.get("currency") == coin:
            outflow += to_decimal(fee.get("cost"))

    return outflow


def detect_quantity_drop(
    previous_balances: dict[str, Decimal],
    current_balances: dict[str, Decimal],
    withdrawals_in_window: list[dict],
    fills_in_window: list[dict],
    tolerance_pct: Decimal,
    tolerance_absolute: Decimal,
    window_start_ms: int | None = None,
    exchange: str = "exchange",
    metrics=None,
    account_label: str | None = None,
) -> list[Detection]:
    """Flags a per-asset quantity decrease not explained by a withdrawal or
    a matching trade fill in the same window. Tracks quantity, not USD
    value, so a price drop is never mistaken for theft.

    `window_start_ms` enforces the "same window" half of that contract. The
    caller is still expected to pass windowed lists, but this filters again
    defensively: passing days of history here used to silently cancel out a
    fresh theft, and the parameter names alone did not stop that happening
    (G-7). When None, no filtering is applied and the caller is trusted.

    `fills_in_window` is expected to be CCXT unified Order records (from
    BitgetClient.fetch_closed_orders), not Trade records - Bitget's
    fetchMyTrades requires a symbol per call on classic accounts, so it
    can't cover "every pair this account touched" in one shot the way
    fetch_closed_orders can. Confirmed against a live account during Phase 1
    testing, not assumed.

    Known Phase 1 limitation, documented rather than silently wrong: this
    does not reconcile futures liquidations, so it should only be wired on
    for spot/hold-style accounts in the pilot. See
    docs/phase0-capability-audit.md section 6 for the leveraged-futures gap
    and the Phase 2 fix (reconcile against contract account-bill liquidation
    entries once that businessType enum is confirmed).

    `exchange` labels the metrics and `account_label` names the account in log
    lines. They were one argument until #55, which meant every undated-record
    counter was keyed by customer instead of by integration."""
    withdrawals = [
        w for w in withdrawals_in_window
        if _in_window(w, window_start_ms, "withdrawal", exchange, metrics, account_label)
    ]
    fills = [
        f for f in fills_in_window
        if _in_window(f, window_start_ms, "fill", exchange, metrics, account_label)
    ]

    # Records just outside the window, used only to report near misses: a
    # detection that a slightly wider settlement grace would have suppressed.
    # The probe re-walks the same records, so it passes no metrics - counting
    # an undated record twice would put a phantom trend under the alarm.
    if window_start_ms is None:
        probe_withdrawals: list[dict] = []
        probe_fills: list[dict] = []
    else:
        probe_start_ms = window_start_ms - NEAR_MISS_PROBE_SECONDS * 1000
        probe_withdrawals = [
            w for w in withdrawals_in_window
            if _in_window(w, probe_start_ms, "withdrawal", exchange, None, account_label)
        ]
        probe_fills = [
            f for f in fills_in_window
            if _in_window(f, probe_start_ms, "fill", exchange, None, account_label)
        ]

    detections = []
    for coin, prev_qty in previous_balances.items():
        curr_qty = current_balances.get(coin, Decimal("0"))
        drop = prev_qty - curr_qty
        if drop <= 0:
            continue

        explained = _explained_outflow_for_coin(coin, withdrawals, fills)
        tolerance = max(tolerance_absolute, prev_qty * tolerance_pct / Decimal("100"))
        unexplained = drop - explained
        if unexplained > tolerance:
            probe_explained = _explained_outflow_for_coin(coin, probe_withdrawals, probe_fills)
            if drop - probe_explained <= tolerance:
                logger.warning(
                    "%s: near miss on %s - this detection would have been suppressed by a "
                    "settlement grace of up to %ds (explained in window: %s, with wider probe: %s). "
                    "Repeated near misses mean the grace is set too tight.",
                    account_label or exchange,
                    coin,
                    NEAR_MISS_PROBE_SECONDS,
                    explained,
                    probe_explained,
                )
            detections.append(
                Detection(
                    rule="unexplained_quantity_decrease",
                    severity="CRITICAL",
                    confidence="high",
                    message=(
                        f"{coin} balance dropped by {drop} but only {explained} is explained "
                        f"by withdrawals/trades in this window ({unexplained} unaccounted for)"
                    ),
                    evidence={
                        "coin": coin,
                        "previous_quantity": str(prev_qty),
                        "current_quantity": str(curr_qty),
                        "drop": str(drop),
                        "explained_outflow": str(explained),
                        "unexplained_amount": str(unexplained),
                    },
                )
            )
    return detections
