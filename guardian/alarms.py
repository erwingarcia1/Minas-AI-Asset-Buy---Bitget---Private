"""In-process alarms on the product's own health (G-48).

An alarm here watches a condition the running process can observe about itself
and raises an operator page through the G-49 channel. It deliberately does not
try to cover the process or the machine dying - nothing in-process can, and an
alarm that depends on the thing it monitors is not an alarm. That needs
external uptime monitoring on separate infrastructure, which is a parked
hosting decision (see #53's scope note).

Why in-process rather than Prometheus rules plus Alertmanager, which #52
recommended: see the issue comment and docs/DECISIONS-NEEDED.md. Short version -
this delivers a tested alarm today without adding two services to a
single-container deployment or duplicating operator credentials into a second
config, and the reusable seam is `OperatorAlerter`, which any future
Alertmanager webhook can also land on.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("guardian.alarms")


class UntimestampedRecordAlarm:
    """Pages when an exchange stops stamping times on the records we rely on.

    The threshold is derived, not chosen. `sentinel_untimestamped_records_total`
    has a resting value of exactly zero - every record an exchange returns
    normally carries a timestamp - so *any* increase is anomalous and there is
    no sensitivity dial to guess at. The only judgment is how long a rise must
    persist before it is a format change rather than a blip, which is what
    `consecutive_windows` is for.

    Tracked per (exchange, record kind): Bitget dropping timestamps on
    withdrawals and Binance dropping them on fills are two different
    investigations, and collapsing them would report one and hide the other.
    """

    RULE = "exchange_records_untimestamped"

    # HIGH rather than CRITICAL, and the reasoning changed while this was being
    # built. #52 was written when an undated record meant a *missed* alert. It
    # no longer does: G-7 makes quantity-drop refuse to credit an undated record
    # as an explanation (so a drop alerts rather than goes quiet), and G-47 makes
    # undated fills judged on first sighting instead of dropped. An exchange
    # dropping timestamps now degrades quality and adds noise; it does not
    # silently stop detection. That is urgent, not wake-at-3am.
    SEVERITY = "HIGH"

    def __init__(self, metrics, operator_alerter, window_seconds: float = 900,
                 consecutive_windows: int = 2):
        self._metrics = metrics
        self._alerter = operator_alerter
        self._window = float(window_seconds)
        self._required = int(consecutive_windows)
        self._last_eval: float | None = None
        self._baseline: dict[tuple[str, str], int] = {}
        self._streak: dict[tuple[str, str], int] = {}

    def evaluate(self, now: float) -> None:
        """Called on the scheduler's cadence, which is faster than the window.

        The window is enforced here rather than assumed from the caller: the
        scheduler ticks every `fast_loop_seconds` (45s by default), so treating
        one tick as one evaluation period would turn "sustained for two periods"
        into ninety seconds instead of the half hour intended."""
        if self._last_eval is None:
            self._last_eval = now
            self._baseline = self._metrics.untimestamped_snapshot()
            return
        if now - self._last_eval < self._window:
            return
        self._last_eval = now

        current = self._metrics.untimestamped_snapshot()
        for key in set(current) | set(self._baseline):
            delta = current.get(key, 0) - self._baseline.get(key, 0)
            if delta > 0:
                self._streak[key] = self._streak.get(key, 0) + 1
                if self._streak[key] >= self._required:
                    self._page(key, delta)
                    # Re-arm rather than latch: the operator cooldown on the
                    # page is what stops this repeating, and it is the dial an
                    # operator can actually turn.
                    self._streak[key] = 0
            else:
                # A format change is continuous. An intermittent record that
                # clears must not accumulate towards a page across unrelated
                # hours.
                self._streak[key] = 0
        self._baseline = current

    def _page(self, key: tuple[str, str], delta: int) -> None:
        exchange, source = key
        minutes = int(self._window * self._required / 60) or 1
        message = (
            f"{exchange} returned {delta} {source} record(s) with no usable timestamp "
            f"in the last window, sustained over {minutes} minutes - its response "
            f"format has probably changed. Detection is degraded, not stopped: "
            f"undated records are refused as explanations (G-7) and undated fills "
            f"are still price-checked (G-47), so expect noise rather than silence."
        )
        logger.error("%s: %s", self.RULE, message)
        if self._alerter is None:
            return
        self._alerter.page(
            rule=self.RULE,
            severity=self.SEVERITY,
            message=message,
            # One rule, no account, many exchanges: without this scope a
            # Binance format change would suppress a concurrent Bitget one.
            dedup_scope=f"{exchange}:{source}",
            evidence={
                "exchange": exchange,
                "source": source,
                "records_in_window": delta,
                "window_seconds": self._window,
                "consecutive_windows": self._required,
                "metric": "sentinel_untimestamped_records_total",
            },
        )
