"""Where the thresholds actually sit, and what the evidence actually says.

Two clusters of surviving mutants from `scripts/mutation-check.sh`, neither of
which any test was holding.

**Boundaries.** Every threshold in this product is a comparison, and every one
of them survived being loosened or tightened by one notch — `>` to `>=`, `<=`
to `<`. Individually each is a hair's difference; the reason they matter is
that a boundary is the one input an attacker can choose. A withdrawal timed to
land exactly on the window edge, a fee at exactly the multiplier, a fill at
exactly the maximum deviation: those are precisely the values a rule is
written to catch, and precisely the ones nothing was checking. The tests below
sit each detector on its own boundary and say which side is inside.

**Evidence.** `fill.get("filled") or fill.get("amount")` mutates to `and` and
survives in two detectors. The alert still fires, so nothing looks broken — it
just reports the quantity the customer *ordered* instead of the quantity that
actually executed. An alert that fires with the wrong number in it is worse
than one that doesn't fire, because someone acts on it.

The SMS tests derive the character budget by probing rather than recomputing
`max_len - len(prefix) - len(action)`. Recomputing it here would mean this file
agrees with `sms.py` by construction and would keep agreeing with it after a
change to either — which is the failure mode this whole file exists to close.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from guardian.alerts.base import AlertMessage
from guardian.alerts.digest import build_digest
from guardian.alerts.sms import format_sms_body
from guardian.detectors.burst import detect_withdrawal_burst
from guardian.detectors.fee_anomaly import detect_fee_anomaly
from guardian.detectors.new_pair import detect_new_pair_activity
from guardian.detectors.off_market import detect_off_market_fills
from guardian.detectors.quantity_drop import NEAR_MISS_PROBE_SECONDS, detect_quantity_drop
from tests.guardian.conftest import make_account
from api.config import settings

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _insert_alert(db, account_id, severity="MEDIUM", rule="abnormal_fee_spend", minutes_ago=60):
    sent_at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, "
            "evidence_json, confidence, sent_at) VALUES (%s, %s, %s, 'k', 'msg', '{}', 'high', %s)",
            (account_id, rule, severity, sent_at),
        )


def _sms(message, max_len):
    alert = AlertMessage(account_label="acct-1", rule="withdrawal_initiated",
                         severity="CRITICAL", confidence="high", message=message, evidence={})
    return format_sms_body(alert, max_len=max_len)


# --- the digest has to say whose alerts these are -------------------------


def test_the_digest_names_each_account_exactly_once(db):
    """`row["label"] != current_label` starts a new section per account.

    Flipped to `==`, `current_label` is never assigned and no account heading
    is ever emitted: the digest becomes an undifferentiated list of events with
    no way to tell which customer any of them belongs to."""
    a = make_account(db, label="acct-a")
    b = make_account(db, label="acct-b")
    _insert_alert(db, a, rule="fee_spend_a", minutes_ago=90)
    _insert_alert(db, a, severity="LOW", rule="drift_a", minutes_ago=60)
    _insert_alert(db, b, rule="fee_spend_b", minutes_ago=30)

    body = build_digest(db, hours=24)

    assert body.count("— acct-a —") == 1
    assert body.count("— acct-b —") == 1
    # And the heading precedes its own account's events, rather than trailing them.
    assert body.index("— acct-a —") < body.index("fee_spend_a")
    assert body.index("— acct-b —") < body.index("fee_spend_b")


# --- SMS: spend the whole budget before cutting ---------------------------


def test_a_message_that_exactly_fits_is_not_truncated(monkeypatch):
    """`len(detail) > room` truncates; at exactly `room` the text fits.

    Loosened to `>=`, the longest message we will send is one character
    shorter than the one that fits, and the customer loses the last character
    of the detail to an ellipsis for no reason."""
    monkeypatch.setattr(settings, "app_base_url", "")
    max_len = 160

    longest = max(n for n in range(1, max_len) if "..." not in _sms("x" * n, max_len))
    body = _sms("x" * longest, max_len)

    # The exactly-fitting message uses every character of the budget.
    assert len(body) == max_len
    assert "x" * longest in body


def test_three_characters_of_room_is_spent_on_nothing_rather_than_an_ellipsis(monkeypatch):
    """`... if room > 3 else ""`.

    With exactly three characters left, the only detail that fits is the
    ellipsis itself. Sending "..." spends the whole remaining budget saying
    that something was omitted without saying anything about it; sending
    nothing leaves the severity, the rule, the account and the log-in
    instruction, which is the part that is acted on. Loosened to `>= 3`, the
    ellipsis comes back."""
    monkeypatch.setattr(settings, "app_base_url", "")
    without_detail = _sms("", 1000)          # prefix + action, nothing else
    room_for_three = len(without_detail) + 3

    assert _sms("a message far too long to fit", room_for_three) == without_detail


# --- burst: the window edge is inside the window --------------------------


def test_a_withdrawal_exactly_on_the_window_edge_counts(monkeypatch):
    """`t >= window_start`.

    Tightened to `>`, a withdrawal landing exactly on the boundary drops out
    of the count. Three withdrawals in ten minutes stops being a burst because
    the first of them was ten minutes ago to the millisecond — and the edge is
    the one value someone draining an account can aim for."""
    times = [NOW - timedelta(minutes=10), NOW - timedelta(minutes=5), NOW]

    detections = detect_withdrawal_burst(times, window_minutes=10, min_count=3, now=NOW)

    assert [d.rule for d in detections] == ["withdrawal_burst"]


# --- fee anomaly: three separate boundaries -------------------------------


def test_spend_exactly_at_the_noise_floor_is_still_examined():
    """`spent < min_fee_floor` skips noise.

    Loosened to `<=`, spend sitting exactly on the floor is skipped instead of
    judged, so an account whose fees jump to precisely the floor is never
    looked at."""
    detections = detect_fee_anomaly(
        window_fees={"USDT": Decimal("1")},
        baseline_ema={"USDT": Decimal("0.01")},
        multiplier=Decimal("3"),
        min_fee_floor=Decimal("1"),
    )

    assert [d.rule for d in detections] == ["abnormal_fee_spend"]


def test_a_zero_baseline_seeds_rather_than_alerting():
    """`baseline is None or baseline <= 0`.

    Tightened to `< 0`, a baseline of exactly zero — which is what every
    account has on its first observation, and what any account that paid no
    fees last window decays towards — stops counting as "no baseline yet".
    The comparison becomes `spent > 0 * multiplier`, so the first fee the
    account ever pays is an anomaly, and every account alerts on day one.

    Zero is not an unlikely value here; it is the starting value."""
    detections = detect_fee_anomaly(
        window_fees={"USDT": Decimal("5")},
        baseline_ema={"USDT": Decimal("0")},
        multiplier=Decimal("3"),
        min_fee_floor=Decimal("1"),
    )

    assert detections == []


def test_spend_exactly_at_the_multiplier_is_not_an_anomaly():
    """`spent > baseline * multiplier`.

    Loosened to `>=`, "more than three times the baseline" becomes "three
    times the baseline", and an account whose fees land exactly on its own
    configured limit gets a MEDIUM for being exactly normal-at-the-edge."""
    detections = detect_fee_anomaly(
        window_fees={"USDT": Decimal("3")},
        baseline_ema={"USDT": Decimal("1")},
        multiplier=Decimal("3"),
        min_fee_floor=Decimal("0.5")
    )

    assert detections == []


# --- off-market: the maximum deviation is still acceptable ----------------


def test_a_fill_exactly_at_the_maximum_deviation_does_not_fire():
    """`deviation_pct > max_deviation_pct`.

    Loosened to `>=`, a fill exactly on the configured maximum fires. The
    setting is named for the largest deviation that is *tolerated*, so the
    value itself has to be inside it — otherwise raising the limit to stop a
    recurring false alarm doesn't stop it."""
    fills = [{"id": "f-1", "symbol": "BTC/USDT", "side": "sell",
              "price": "110", "filled": "1", "timestamp": 1}]

    detections = detect_off_market_fills(
        fills, {"BTC/USDT": Decimal("100")}, max_deviation_pct=Decimal("10")
    )

    assert detections == []


# --- evidence reports what executed, not what was asked for ---------------


def test_off_market_evidence_reports_the_executed_quantity():
    """`fill.get("filled") or fill.get("amount")`.

    Mutated to `and`, the evidence carries `amount` — the size of the *order*
    — whenever `filled` is present. A partially filled order then reports the
    full order size, and the alert overstates what moved. The alert still
    fires, so nothing looks wrong; it just has the wrong number in it, and the
    number is what the recipient acts on."""
    fills = [{"id": "f-1", "symbol": "BTC/USDT", "side": "sell",
              "price": "50", "filled": "2", "amount": "99", "timestamp": 1}]

    detections = detect_off_market_fills(
        fills, {"BTC/USDT": Decimal("100")}, max_deviation_pct=Decimal("10")
    )

    assert detections[0].evidence["amount"] == "2"


def test_off_market_evidence_falls_back_to_the_order_size():
    """The `or` also has to keep working as a fallback: an exchange that
    reports no `filled` at all must still put a quantity in the evidence."""
    fills = [{"id": "f-1", "symbol": "BTC/USDT", "side": "sell",
              "price": "50", "amount": "7", "timestamp": 1}]

    detections = detect_off_market_fills(
        fills, {"BTC/USDT": Decimal("100")}, max_deviation_pct=Decimal("10")
    )

    assert detections[0].evidence["amount"] == "7"


def test_new_pair_evidence_reports_the_executed_quantity():
    """Same mutation, same consequence, in the other detector that carries it.
    A first-ever trade on an unknown pair is the alert that gets read most
    carefully, so the size in it has to be the size that traded."""
    fills = [{"id": "f-1", "symbol": "XYZ/USDT", "side": "buy",
              "price": "1", "filled": "2", "amount": "99", "timestamp": 1}]

    detections = detect_new_pair_activity(fills, known_symbols={"BTC/USDT"})

    assert [d.rule for d in detections] == ["never_touched_pair_activity"]
    assert detections[0].evidence["amount"] == "2"


# --- quantity drop: the tolerance is tolerated ----------------------------


def test_an_unexplained_amount_exactly_at_the_tolerance_does_not_alert():
    """`unexplained > tolerance`.

    Loosened to `>=`, a shortfall of exactly the tolerance becomes a CRITICAL.
    Tolerance exists to absorb rounding and settlement timing, both of which
    land on round numbers constantly, so this is the difference between a
    quiet account and a nightly page."""
    detections = detect_quantity_drop(
        previous_balances={"BTC": Decimal("100")},
        current_balances={"BTC": Decimal("99")},
        withdrawals_in_window=[],
        fills_in_window=[],
        tolerance_pct=Decimal("0"),
        tolerance_absolute=Decimal("1"),
    )

    assert detections == []


def test_a_near_miss_exactly_at_the_tolerance_is_logged_for_the_operator(caplog):
    """`drop - probe_explained <= tolerance` guards the near-miss warning.

    This branch changes no alert — the detection fires either way. What it
    changes is whether we are told that the detection would have been
    suppressed by a slightly longer settlement grace. That log line is the
    only evidence that the grace is set too tight, and the boundary case is
    exactly the one a marginal setting produces. Tightened to `<`, the near
    miss at precisely the tolerance goes unreported and the setting looks fine
    while it keeps firing."""
    window_start_ms = 1_000_000_000
    # A withdrawal that settled just before the window opened: invisible to
    # the detector, visible to the wider probe.
    just_outside = window_start_ms - (NEAR_MISS_PROBE_SECONDS - 1) * 1000
    withdrawal = {"id": "w-1", "currency": "BTC", "amount": "9",
                  "timestamp": just_outside, "status": "ok"}

    with caplog.at_level(logging.WARNING, logger="guardian.detectors.quantity_drop"):
        detections = detect_quantity_drop(
            previous_balances={"BTC": Decimal("100")},
            current_balances={"BTC": Decimal("90")},   # drop of 10
            withdrawals_in_window=[withdrawal],        # filtered out by the window
            fills_in_window=[],
            tolerance_pct=Decimal("0"),
            tolerance_absolute=Decimal("1"),           # 10 - 9 == tolerance exactly
            window_start_ms=window_start_ms,
        )

    assert [d.rule for d in detections] == ["unexplained_quantity_decrease"]
    assert any("near miss" in r.getMessage() for r in caplog.records)
