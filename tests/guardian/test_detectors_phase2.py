from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from guardian.detectors.burst import detect_withdrawal_burst
from guardian.detectors.fee_anomaly import detect_fee_anomaly, sum_fees_by_coin, update_ema
from guardian.detectors.heartbeat import detect_baseline_drift, detect_unreachable
from guardian.detectors.new_pair import detect_new_pair_activity
from guardian.detectors.off_market import detect_off_market_fills
from guardian.detectors.transfer_pattern import detect_new_transfer_route

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


# --- withdrawal burst (structuring) ---------------------------------------

def test_burst_triggers_on_min_count_within_window():
    times = [NOW - timedelta(minutes=m) for m in (5, 20, 40)]
    detections = detect_withdrawal_burst(times, window_minutes=60, min_count=3, now=NOW)
    assert len(detections) == 1
    assert detections[0].rule == "withdrawal_burst"
    assert detections[0].severity == "HIGH"


def test_burst_not_triggered_when_spread_beyond_window():
    times = [NOW - timedelta(minutes=m) for m in (5, 90, 200)]
    assert detect_withdrawal_burst(times, window_minutes=60, min_count=3, now=NOW) == []


# --- transfer route pattern -------------------------------------------------

def test_new_transfer_route_triggers_high():
    transfers = [{"id": "t1", "fromAccount": "spot", "toAccount": "swap", "currency": "USDT", "amount": 100}]
    detections = detect_new_transfer_route(transfers, known_routes=set())
    assert len(detections) == 1
    assert detections[0].rule == "transfer_route_never_seen"


def test_known_transfer_route_does_not_trigger():
    transfers = [{"id": "t1", "fromAccount": "spot", "toAccount": "swap", "currency": "USDT", "amount": 100}]
    assert detect_new_transfer_route(transfers, known_routes={"spot->swap"}) == []


def test_same_new_route_fires_once_per_batch():
    transfers = [
        {"id": "t1", "fromAccount": "spot", "toAccount": "swap"},
        {"id": "t2", "fromAccount": "spot", "toAccount": "swap"},
    ]
    assert len(detect_new_transfer_route(transfers, known_routes=set())) == 1


# --- never-touched pair -------------------------------------------------------

def test_new_pair_triggers_high():
    fills = [{"symbol": "SHIB/USDT", "side": "buy", "amount": 1000, "timestamp": 1}]
    detections = detect_new_pair_activity(fills, known_symbols={"BTC/USDT"})
    assert len(detections) == 1
    assert detections[0].rule == "never_touched_pair_activity"


def test_known_pair_does_not_trigger():
    fills = [{"symbol": "BTC/USDT", "side": "buy", "amount": 1, "timestamp": 1}]
    assert detect_new_pair_activity(fills, known_symbols={"BTC/USDT"}) == []


# --- off-market execution -----------------------------------------------------

def test_off_market_fill_triggers_high():
    fills = [{"symbol": "XYZ/USDT", "average": "0.5", "side": "sell", "amount": 1000, "id": "o1"}]
    refs = {"XYZ/USDT": Decimal("1.0")}  # fill at 50% below reference
    detections = detect_off_market_fills(fills, refs, max_deviation_pct=Decimal("10"))
    assert len(detections) == 1
    assert detections[0].rule == "off_market_execution"


def test_at_market_fill_does_not_trigger():
    fills = [{"symbol": "XYZ/USDT", "average": "0.99", "side": "sell", "amount": 10, "id": "o2"}]
    refs = {"XYZ/USDT": Decimal("1.0")}
    assert detect_off_market_fills(fills, refs, max_deviation_pct=Decimal("10")) == []


def test_fill_without_reference_price_is_skipped():
    fills = [{"symbol": "NOREF/USDT", "average": "0.5", "side": "sell", "amount": 10, "id": "o3"}]
    assert detect_off_market_fills(fills, {}, max_deviation_pct=Decimal("10")) == []


# --- fee anomaly ----------------------------------------------------------------

def test_fee_spike_over_baseline_triggers_medium():
    window = {"USDT": Decimal("50")}
    baseline = {"USDT": Decimal("5")}
    detections = detect_fee_anomaly(window, baseline, multiplier=Decimal("5"), min_fee_floor=Decimal("1"))
    assert len(detections) == 1
    assert detections[0].rule == "abnormal_fee_spend"
    assert detections[0].severity == "MEDIUM"


def test_fee_within_baseline_does_not_trigger():
    window = {"USDT": Decimal("6")}
    baseline = {"USDT": Decimal("5")}
    assert detect_fee_anomaly(window, baseline, Decimal("5"), Decimal("1")) == []


def test_fee_below_floor_is_suppressed_even_if_many_multiples():
    window = {"USDT": Decimal("0.05")}
    baseline = {"USDT": Decimal("0.001")}
    assert detect_fee_anomaly(window, baseline, Decimal("5"), Decimal("1")) == []


def test_fee_helpers():
    fills = [
        {"fee": {"currency": "USDT", "cost": "1.5"}},
        {"fee": {"currency": "USDT", "cost": "2.5"}},
        {"fee": {"currency": "BTC", "cost": "0.0001"}},
    ]
    fees = sum_fees_by_coin(fills)
    assert fees["USDT"] == Decimal("4.0")
    assert update_ema(None, Decimal("10"), Decimal("0.2")) == Decimal("10")
    assert update_ema(Decimal("10"), Decimal("20"), Decimal("0.5")) == Decimal("15.0")


# --- heartbeat + drift ------------------------------------------------------------

def test_unreachable_triggers_at_threshold():
    detections = detect_unreachable(consecutive_failures=5, threshold=5)
    assert len(detections) == 1
    assert detections[0].rule == "account_unreachable"


def test_reachable_below_threshold_does_not_trigger():
    assert detect_unreachable(consecutive_failures=4, threshold=5) == []


def test_baseline_drift_fires_low_on_new_deposit_source():
    detections = detect_baseline_drift(["bc1qnewsource"])
    assert len(detections) == 1
    assert detections[0].severity == "LOW"


def test_no_drift_no_detection():
    assert detect_baseline_drift([]) == []
