"""A record a detector skips must not stop it looking at the rest.

Eight `continue` → `break` mutants survived `scripts/mutation-check.sh` — one in
almost every detector that loops. Not a single test anywhere distinguished
"skip this record" from "stop scanning".

The consequence is the widest of any survivor class: one unremarkable record
early in a batch silences every check after it, and the silence is total. No
error, no log line, no partial result — the loop simply ends and the detector
reports nothing found.

`off_market` is the one that is provably reachable rather than theoretical.
`reference_prices` only contains symbols whose ticker fetch succeeded, and the
per-symbol fallback in the poller is capped at ten symbols — so on an account
trading eleven pairs there is always a fill with no reference price, and under
the mutation every fill after it goes unchecked.

Each test below puts a skippable record FIRST and a detectable one SECOND, which
is the only arrangement that tells the two behaviours apart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from guardian.detectors.fee_anomaly import detect_fee_anomaly
from guardian.detectors.new_pair import detect_new_pair_activity
from guardian.detectors.off_market import detect_off_market_fills
from guardian.detectors.quantity_drop import detect_quantity_drop
from guardian.detectors.transfer_pattern import detect_new_transfer_route
from guardian.detectors.unseen_address import detect_unseen_address

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def test_off_market_keeps_checking_after_a_fill_with_no_reference_price():
    """The reachable one: an eleventh symbol always lacks a reference."""
    fills = [
        {"symbol": "NOREF/USDT", "average": "1", "side": "sell", "amount": "1", "id": "a"},
        {"symbol": "XYZ/USDT", "average": "0.5", "side": "sell", "amount": "1000", "id": "b"},
    ]
    detections = detect_off_market_fills(fills, {"XYZ/USDT": Decimal("1.0")}, Decimal("10"))
    assert [d.evidence["order_id"] for d in detections] == ["b"]


def test_off_market_keeps_checking_after_a_fill_with_no_usable_price():
    fills = [
        {"symbol": "XYZ/USDT", "average": "0", "price": "0", "side": "sell", "amount": "1", "id": "a"},
        {"symbol": "XYZ/USDT", "average": "0.5", "side": "sell", "amount": "1000", "id": "b"},
    ]
    detections = detect_off_market_fills(fills, {"XYZ/USDT": Decimal("1.0")}, Decimal("10"))
    assert [d.evidence["order_id"] for d in detections] == ["b"]


def test_new_pair_keeps_checking_after_a_fill_with_no_symbol():
    fills = [
        {"side": "buy", "amount": 1, "timestamp": 1},
        {"symbol": "SHIB/USDT", "side": "buy", "amount": 1000, "timestamp": 2},
    ]
    detections = detect_new_pair_activity(fills, known_symbols={"BTC/USDT"})
    assert [d.evidence["symbol"] for d in detections] == ["SHIB/USDT"]


def test_unseen_address_keeps_checking_after_a_withdrawal_with_no_address():
    """The address-less record produces its own degraded LOW detection and the
    scan continues; under the mutation the CRITICAL after it never fires."""
    records = [
        {"id": "w1", "currency": "BTC", "amount": "1"},
        {"id": "w2", "currency": "BTC", "amount": "2", "address": "bc1qneverseen"},
    ]
    rules = [d.rule for d in detect_unseen_address(records, known_addresses={"bc1qknown"})]
    assert "withdrawal_to_unseen_address" in rules


def test_transfer_route_keeps_checking_after_a_route_already_known():
    transfers = [
        {"id": "t1", "fromAccount": "spot", "toAccount": "swap"},
        {"id": "t2", "fromAccount": "spot", "toAccount": "p2p"},
    ]
    detections = detect_new_transfer_route(transfers, known_routes={"spot->swap"})
    assert [d.evidence["route"] for d in detections] == ["spot->p2p"]


def test_fee_anomaly_keeps_checking_after_a_coin_below_the_floor():
    """`spent < min_fee_floor` → continue. Under the mutation a single
    below-floor coin ends the scan, so the coin being churned goes unreported."""
    detections = detect_fee_anomaly(
        window_fees={"AAA": Decimal("0.001"), "USDT": Decimal("500")},
        baseline_ema={"AAA": Decimal("0.001"), "USDT": Decimal("2")},
        multiplier=Decimal("5"),
        min_fee_floor=Decimal("1"),
    )
    assert [d.evidence["coin"] for d in detections] == ["USDT"]


def test_fee_anomaly_keeps_checking_after_a_coin_with_no_baseline():
    """`baseline is None` → continue: a first-seen coin seeds rather than
    alerts, and must not stop the coins that do have a baseline."""
    detections = detect_fee_anomaly(
        window_fees={"NEW": Decimal("50"), "USDT": Decimal("500")},
        baseline_ema={"USDT": Decimal("2")},
        multiplier=Decimal("5"),
        min_fee_floor=Decimal("1"),
    )
    assert [d.evidence["coin"] for d in detections] == ["USDT"]


def test_quantity_drop_keeps_checking_after_a_coin_that_did_not_drop():
    """`drop <= 0` → continue. A coin that rose would otherwise end the scan,
    and every coin after it - including one drained to zero - is never
    examined. Dict order is insertion order, so the riser genuinely comes
    first."""
    detections = detect_quantity_drop(
        previous_balances={"ETH": Decimal("1"), "BTC": Decimal("10")},
        current_balances={"ETH": Decimal("5"), "BTC": Decimal("0")},
        withdrawals_in_window=[],
        fills_in_window=[],
        tolerance_pct=Decimal("0.5"),
        tolerance_absolute=Decimal("0.00000001"),
    )
    assert [d.evidence["coin"] for d in detections] == ["BTC"]


def test_quantity_drop_keeps_checking_after_a_record_outside_the_window():
    """`_in_window` filters with a comprehension, but the near-miss probe and
    the main filter both walk every record - a stale one must not truncate
    either."""
    window_start = int((NOW - timedelta(minutes=5)).timestamp() * 1000)
    stale = int((NOW - timedelta(days=30)).timestamp() * 1000)
    fresh = int((NOW - timedelta(minutes=1)).timestamp() * 1000)

    detections = detect_quantity_drop(
        previous_balances={"BTC": Decimal("10")},
        current_balances={"BTC": Decimal("5")},
        withdrawals_in_window=[
            {"id": "old", "currency": "BTC", "amount": "5", "timestamp": stale},
            {"id": "new", "currency": "BTC", "amount": "5", "timestamp": fresh},
        ],
        fills_in_window=[],
        tolerance_pct=Decimal("0.5"),
        tolerance_absolute=Decimal("0.00000001"),
        window_start_ms=window_start,
    )
    # The fresh withdrawal explains the whole drop; the stale one is excluded.
    assert detections == []
