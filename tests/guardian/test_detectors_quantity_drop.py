from __future__ import annotations

from decimal import Decimal

from guardian.detectors.quantity_drop import detect_quantity_drop
from tests.guardian.fixtures.ccxt_responses import trade_record, withdrawal_record

TOL_PCT = Decimal("0.5")
TOL_ABS = Decimal("0.00000001")


def test_unexplained_drop_triggers_critical():
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    detections = detect_quantity_drop(previous, current, [], [], TOL_PCT, TOL_ABS)
    assert len(detections) == 1
    assert detections[0].rule == "unexplained_quantity_decrease"
    assert detections[0].severity == "CRITICAL"


def test_drop_explained_by_withdrawal_does_not_trigger():
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    wd = withdrawal_record(amount="0.4999", currency="BTC")  # + 0.0001 fee = 0.5
    detections = detect_quantity_drop(previous, current, [wd], [], TOL_PCT, TOL_ABS)
    assert detections == []


def test_drop_explained_by_sell_trade_does_not_trigger():
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.9")}
    trade = trade_record(symbol="BTC/USDT", side="sell", amount="0.1", cost="6000")
    detections = detect_quantity_drop(previous, current, [], [trade], TOL_PCT, TOL_ABS)
    assert detections == []


def test_price_drop_alone_does_not_trigger():
    # Quantity unchanged - a price crash must never look like theft, since
    # this detector tracks per-asset quantity, not USD value.
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("1.0")}
    detections = detect_quantity_drop(previous, current, [], [], TOL_PCT, TOL_ABS)
    assert detections == []


# --- Windowing (G-7) -------------------------------------------------------
# The four tests above pass a window-less list and are deliberately left
# unchanged: they cover the arithmetic, not the window. These call the
# detector directly with window_start_ms, which is the defence that stops a
# caller handing over days of history again.

NOW_MS = 1_700_000_000_000
MINUTE_MS = 60_000


def _ts(record, ms):
    record["timestamp"] = ms
    return record


def test_fill_before_window_does_not_explain_drop():
    """The G-7 bug in miniature: a week-old sale must not cancel a fresh drop."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    stale = _ts(trade_record(symbol="BTC/USDT", side="sell", amount="0.5"), NOW_MS - 7 * 24 * 60 * MINUTE_MS)
    detections = detect_quantity_drop(
        previous, current, [], [stale], TOL_PCT, TOL_ABS, window_start_ms=NOW_MS - MINUTE_MS
    )
    assert len(detections) == 1
    assert detections[0].rule == "unexplained_quantity_decrease"
    assert detections[0].evidence["explained_outflow"] == "0"


def test_fill_inside_window_still_explains_drop():
    """The windowing must not break the legitimate case it exists to protect."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    fresh = _ts(trade_record(symbol="BTC/USDT", side="sell", amount="0.5"), NOW_MS - 10_000)
    detections = detect_quantity_drop(
        previous, current, [], [fresh], TOL_PCT, TOL_ABS, window_start_ms=NOW_MS - MINUTE_MS
    )
    assert detections == []


def test_withdrawal_before_window_does_not_explain_drop():
    """Withdrawals are windowed on the same terms as fills."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    stale = withdrawal_record(amount="0.4999", currency="BTC", timestamp=NOW_MS - 7 * 24 * 60 * MINUTE_MS)
    detections = detect_quantity_drop(
        previous, current, [stale], [], TOL_PCT, TOL_ABS, window_start_ms=NOW_MS - MINUTE_MS
    )
    assert len(detections) == 1


def test_fill_exactly_at_window_start_explains_drop():
    """The boundary is inclusive - a fill at the snapshot instant counts."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    boundary = _ts(trade_record(symbol="BTC/USDT", side="sell", amount="0.5"), NOW_MS - MINUTE_MS)
    detections = detect_quantity_drop(
        previous, current, [], [boundary], TOL_PCT, TOL_ABS, window_start_ms=NOW_MS - MINUTE_MS
    )
    assert detections == []


def test_untimestamped_fill_does_not_explain_drop(caplog):
    """A record we can't place in time must not be assumed safe, and the
    exclusion must be logged with the exchange so chronic omissions surface
    as data rather than as a customer muting their alerts."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    undated = trade_record(symbol="BTC/USDT", side="sell", amount="0.5")
    undated.pop("timestamp")

    with caplog.at_level("WARNING"):
        detections = detect_quantity_drop(
            previous, current, [], [undated], TOL_PCT, TOL_ABS,
            window_start_ms=NOW_MS - MINUTE_MS, exchange="bitget-main",
        )

    assert len(detections) == 1
    assert "bitget-main" in caplog.text
    assert "no timestamp" in caplog.text


def test_unparseable_timestamp_does_not_explain_drop(caplog):
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    bad = _ts(trade_record(symbol="BTC/USDT", side="sell", amount="0.5"), "not-a-number")

    with caplog.at_level("WARNING"):
        detections = detect_quantity_drop(
            previous, current, [], [bad], TOL_PCT, TOL_ABS,
            window_start_ms=NOW_MS - MINUTE_MS, exchange="bybit-main",
        )

    assert len(detections) == 1
    assert "bybit-main" in caplog.text
    assert "unparseable" in caplog.text


def test_near_miss_is_logged_when_a_wider_grace_would_suppress(caplog):
    """A fill just outside the window still alerts, but logs evidence that the
    grace may be too tight - so tuning comes from data, not guesswork."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    just_outside = _ts(trade_record(symbol="BTC/USDT", side="sell", amount="0.5"), NOW_MS - 2 * MINUTE_MS)

    with caplog.at_level("WARNING"):
        detections = detect_quantity_drop(
            previous, current, [], [just_outside], TOL_PCT, TOL_ABS,
            window_start_ms=NOW_MS - MINUTE_MS, exchange="okx-main",
        )

    assert len(detections) == 1, "a near miss must still alert, not be suppressed"
    assert "near miss" in caplog.text
    assert "okx-main" in caplog.text


def test_no_near_miss_log_for_a_genuinely_unexplained_drop(caplog):
    """A drop with no explaining activity at all is not a near miss, and must
    not emit a tuning hint that would train an operator to widen the grace."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}

    with caplog.at_level("WARNING"):
        detections = detect_quantity_drop(
            previous, current, [], [], TOL_PCT, TOL_ABS, window_start_ms=NOW_MS - MINUTE_MS
        )

    assert len(detections) == 1
    assert "near miss" not in caplog.text


# --- Untimestamped-record metric -------------------------------------------
# The log line above proves the exclusion is *recorded*; these prove it is
# *countable*. A format change on the exchange side has to reach an operator
# through an alarm, not through someone happening to read the logs.


class _RecordingMetrics:
    """Stands in for sentinel.metrics.Metrics - the detector only ever calls
    untimestamped_record on it, and this pins that surface down."""

    def __init__(self):
        self.calls = []

    def untimestamped_record(self, exchange, source):
        self.calls.append((exchange, source))


def test_untimestamped_records_are_counted_per_exchange_and_source():
    previous = {"BTC": Decimal("1.0"), "ETH": Decimal("10")}
    current = {"BTC": Decimal("0.5"), "ETH": Decimal("5")}
    undated_fill = trade_record(symbol="BTC/USDT", side="sell", amount="0.5")
    undated_fill.pop("timestamp")
    unparseable_withdrawal = withdrawal_record(amount="5", currency="ETH", timestamp="not-a-number")

    metrics = _RecordingMetrics()
    detect_quantity_drop(
        previous, current, [unparseable_withdrawal], [undated_fill], TOL_PCT, TOL_ABS,
        window_start_ms=NOW_MS - MINUTE_MS, exchange="binance-main", metrics=metrics,
    )

    assert metrics.calls == [("binance-main", "withdrawal"), ("binance-main", "fill")]


def test_undated_record_is_counted_once_not_once_per_pass():
    """The near-miss probe re-walks the same records. If it also counted them,
    a single undated fill would read as two on the alarm - a phantom trend,
    and the alarm's threshold would be calibrated against a lie."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    undated = trade_record(symbol="BTC/USDT", side="sell", amount="0.5")
    undated.pop("timestamp")

    metrics = _RecordingMetrics()
    detect_quantity_drop(
        previous, current, [], [undated], TOL_PCT, TOL_ABS,
        window_start_ms=NOW_MS - MINUTE_MS, exchange="binance-main", metrics=metrics,
    )

    assert metrics.calls == [("binance-main", "fill")]


def test_dated_records_are_never_counted():
    """The counter must mean 'the exchange stopped stamping times', nothing
    else. A perfectly dated record outside the window is not that."""
    previous = {"BTC": Decimal("1.0")}
    current = {"BTC": Decimal("0.5")}
    stale = _ts(trade_record(symbol="BTC/USDT", side="sell", amount="0.5"), NOW_MS - 7 * 24 * 60 * MINUTE_MS)

    metrics = _RecordingMetrics()
    detect_quantity_drop(
        previous, current, [], [stale], TOL_PCT, TOL_ABS,
        window_start_ms=NOW_MS - MINUTE_MS, exchange="binance-main", metrics=metrics,
    )

    assert metrics.calls == []


# --- the warning has to say WHICH record (#70) --------------------------------
#
# Six surviving mutants were all the same expression: the `or` chain choosing
# what to call the record in these two `logger.warning` calls. They change no
# behaviour - the record is excluded either way, and the metric an operator is
# alarmed on is pinned elsewhere - so the question they pose is a decision
# rather than a defect: is this line supposed to identify the record?
#
# It is. The metric says an exchange stopped stamping times; this line is where
# the operator who was paged goes next, and a warning that cannot say which
# record lost its timestamp gives them nothing to look at. The property is
# pinned, not the wording: an id when there is one, the symbol when there is
# not, and an explicit "<unidentified>" when there is neither - because a
# message that trails off after "fill" reads like a truncation bug.


def _drop_with(record, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    caplog.clear()
    detect_quantity_drop(
        {"BTC": Decimal("1.0")}, {"BTC": Decimal("0.5")}, [], [record], TOL_PCT, TOL_ABS,
        window_start_ms=NOW_MS - MINUTE_MS, exchange="binance", account_label="acct-1",
    )
    return caplog.text


def test_an_undated_record_is_named_by_its_id(caplog):
    fill = trade_record(symbol="BTC/USDT", side="sell", amount="0.5")
    fill["id"] = "fill-42"
    fill.pop("timestamp", None)

    assert "fill-42" in _drop_with(fill, caplog)


def test_an_undated_record_with_no_id_is_named_by_its_symbol(caplog):
    fill = trade_record(symbol="BTC/USDT", side="sell", amount="0.5")
    fill.pop("id", None)
    fill.pop("timestamp", None)

    text = _drop_with(fill, caplog)
    assert "BTC/USDT" in text
    assert "<unidentified>" not in text


def test_an_undated_record_with_neither_says_so_explicitly(caplog):
    fill = trade_record(side="sell", amount="0.5")
    fill.pop("id", None)
    fill.pop("symbol", None)
    fill.pop("timestamp", None)

    assert "<unidentified>" in _drop_with(fill, caplog)


def test_the_same_holds_for_an_unparseable_timestamp(caplog):
    """The second warning, which is a separate copy of the same expression -
    and so a separate set of survivors."""
    fill = trade_record(symbol="BTC/USDT", side="sell", amount="0.5")
    fill["id"] = "fill-99"
    fill["timestamp"] = "not-a-number"

    text = _drop_with(fill, caplog)
    assert "fill-99" in text
    assert "unparseable" in text

    nameless = trade_record(side="sell", amount="0.5")
    nameless.pop("id", None)
    nameless.pop("symbol", None)
    nameless["timestamp"] = "not-a-number"

    assert "<unidentified>" in _drop_with(nameless, caplog)
