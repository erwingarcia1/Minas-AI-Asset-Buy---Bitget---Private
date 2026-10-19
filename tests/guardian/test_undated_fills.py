"""G-47 (#51): a fill the exchange didn't stamp with a time must still be
price-checked and fee-checked - exactly once.

The old filter kept only fills newer than the previous fast loop, and read a
missing timestamp as `0`. `0 >= last_run_ms` is false, so an undated fill was
dropped from the only list that feeds off-market and fee-anomaly checking. It
was never examined - not on that cycle, not on any later one - while the same
fill still reached the new-pair detector and the traded-pair baseline. The
account looked watched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from guardian import poller as poller_module
from guardian.config import RulesConfig
from guardian.onboarding import onboard_account
from guardian.poller import AccountPoller
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import SHIPPED_RULES


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _client(fills=None, balance_total=None, tickers=None):
    client = MagicMock()
    client.fetch_deposits.return_value = []
    client.fetch_withdrawals.return_value = []
    client.fetch_closed_orders.return_value = fills or []
    client.fetch_balance.return_value = {"total": balance_total or {"USDT": 1000.0}}
    client.fetch_account_info.return_value = account_info_response()
    client.fetch_tickers.return_value = tickers or {}
    client.fetch_transfers.return_value = []
    return client


def _fill(order_id="f-1", symbol="XYZ/USDT", price="0.5", timestamp=None, fee_cost="1.0"):
    """A CCXT unified order record. `timestamp=None` is the case under test:
    the exchange returned the fill without one."""
    record = {
        "id": order_id,
        "symbol": symbol,
        "side": "sell",
        "average": price,
        "price": price,
        "amount": "1000",
        "filled": "1000",
        "cost": "500",
        "fee": {"currency": "USDT", "cost": fee_cost},
        "timestamp": timestamp,
        "info": {},
    }
    return record


def _ticker(symbol="XYZ/USDT", last="1.0"):
    return {symbol: {"symbol": symbol, "last": last, "close": last}}


def _poller(db, cipher, channel, metrics=None):
    rules = RulesConfig(SHIPPED_RULES)
    return AccountPoller(
        db,
        cipher,
        rules,
        {"CRITICAL": [channel], "HIGH": [channel], "MEDIUM": [channel], "LOW": [channel]},
        metrics=metrics,
    )


def _settled_account(db, cipher, monkeypatch, label="undated-1"):
    """Onboards an account and runs one quiet cycle, so `last_fast_loop_at` is
    set. Without that the poller's old code path judged every fill regardless
    of timestamp, and the defect wouldn't show."""
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label=label, topology="independent", api_key="k", secret="s", passphrase="p"
    )
    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: _client())
    quiet = _poller(db, cipher, RecordingChannel())
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        account_row = cur.fetchone()
    quiet.run_fast_loop(account_row)
    return result.account_id, account_row


def test_undated_off_market_fill_still_raises_the_alert(db, cipher, monkeypatch):
    """AC: a fill with a missing timestamp is still checked for off-market
    execution. Sold at 0.5 against a 1.0 reference - 50% away, five times the
    10% threshold."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch)

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(timestamp=None)], tickers=_ticker()),
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    assert "off_market_execution" in {a.rule for a in channel.sent}


def test_unparseable_timestamp_is_treated_as_undated_not_as_zero(db, cipher, monkeypatch):
    """AC: *unparseable*, not only missing. A timestamp of "" or a non-numeric
    string must not quietly become the epoch and drop the fill."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch, label="undated-2")

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(timestamp="not-a-time")], tickers=_ticker()),
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    assert "off_market_execution" in {a.rule for a in channel.sent}


def test_undated_fill_is_judged_once_not_every_cycle(db, cipher, monkeypatch):
    """AC: checked once. The same undated fill comes back in the exchange's
    response on every poll - it must not be re-examined.

    Asserts on what the detector is *handed*, not on how many alerts came out
    the other end. The earlier form of this test counted delivered
    `off_market_execution` alerts across three cycles and expected one - which
    is also what you get with the identity gate deleted entirely, because
    dispatch suppresses a repeat of the same HIGH alert for 30 minutes. It
    passed on the cooldown, not on the guarantee it names, and a design-guard
    probe that removed `if record_seen_fill(...)` left it green.

    `detect_off_market_fills` is only called when the window is non-empty, so
    the batches it receives are a direct read of what entered the window."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch, label="undated-3")

    batches: list[list] = []
    real_detect = poller_module.detect_off_market_fills

    def recording(fills, *args, **kwargs):
        batches.append([f.get("id") for f in fills])
        return real_detect(fills, *args, **kwargs)

    monkeypatch.setattr("guardian.poller.detect_off_market_fills", recording)
    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(timestamp=None)], tickers=_ticker()),
    )
    channel = RecordingChannel()
    poller = _poller(db, cipher, channel)
    poller.run_fast_loop(account_row)
    poller.run_fast_loop(account_row)
    poller.run_fast_loop(account_row)

    assert batches == [["f-1"]], (
        f"the fill entered the examination window {len(batches)} times, expected once: {batches}"
    )
    off_market = [a for a in channel.sent if a.rule == "off_market_execution"]
    assert len(off_market) == 1, f"alerted {len(off_market)} times, expected once"


def test_undated_fill_fee_enters_the_baseline_once(db, cipher, monkeypatch):
    """AC: not re-counted into the fee baseline every cycle. Re-adding the
    same fee every 45 seconds walks the EMA up until the fee detector can
    never fire again - a detector that silently stops detecting."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch, label="undated-4")

    from guardian.baseline import load_fee_baseline, save_fee_baseline

    # Seed the EMA away from the fee under test, which is the whole reason this
    # test can detect anything. `update_ema` is
    # `alpha * observation + (1 - alpha) * previous`, so observing a fee equal
    # to the current EMA leaves it exactly where it was: with the baseline
    # seeded at 10 and the fill's fee also 10, re-counting that fill on every
    # cycle moved the number not at all, and the assertion below held whether
    # or not the guarantee did. A probe deleting the seen-fill gate left this
    # test green. Seeded at 100, one sighting gives 0.2*10 + 0.8*100 = 82 and
    # each re-count walks it further towards 10.
    save_fee_baseline(db, account_id, {"USDT": Decimal("100")})

    # At-market price, so only the fee path is exercised.
    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(
            fills=[_fill(timestamp=None, price="1.0", fee_cost="10")],
            tickers=_ticker(),
        ),
    )
    poller = _poller(db, cipher, RecordingChannel())
    poller.run_fast_loop(account_row)

    after_first = load_fee_baseline(db, account_id).get("USDT")
    assert after_first == Decimal("82"), f"first sighting should move the EMA to 82, got {after_first}"

    for _ in range(5):
        poller.run_fast_loop(account_row)

    after_repeats = load_fee_baseline(db, account_id).get("USDT")
    assert after_repeats == after_first, (
        f"the same fill's fee was counted again: baseline moved {after_first} -> {after_repeats}"
    )


def test_undated_fill_is_counted_on_the_untimestamped_metric_exactly_once_per_pass(
    db, cipher, monkeypatch
):
    """AC: counted and surfaced, not silently handled. An exchange that starts
    omitting timestamps has to be visible to an operator as a rising counter,
    on the same metric G-7 established.

    The count belongs to the quantity-drop path, which already walks every
    fill and already emits `source="fill"`. The off-market/fee path must NOT
    add a second increment for the same record on the same pass: that puts a
    phantom trend under the alarm and calibrates its threshold against a lie
    (the reasoning `163021e` used to keep the near-miss probe silent). So this
    pins the count at exactly one per undated fill per cycle - it fails both
    if the record stops being counted and if it starts being counted twice."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch, label="undated-5")

    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(timestamp=None)], tickers=_ticker()),
    )
    metrics = MagicMock()
    _poller(db, cipher, RecordingChannel(), metrics=metrics).run_fast_loop(account_row)

    sources = [c.args[1] if len(c.args) > 1 else c.kwargs.get("source")
               for c in metrics.untimestamped_record.call_args_list]
    fill_counts = [s for s in sources if s == "fill"]
    assert len(fill_counts) == 1, (
        f"expected the undated fill counted exactly once this pass, got {len(fill_counts)}; "
        f"sources seen: {sources}"
    )


def test_dated_fill_from_before_the_last_poll_is_not_rejudged(db, cipher, monkeypatch):
    """Regression guard on the other side: identity tracking must not start
    re-examining backlog the time window used to exclude. An old dated fill
    would be compared against *today's* ticker, which is not evidence of
    anything."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch, label="undated-6")

    old_ms = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(timestamp=old_ms)], tickers=_ticker()),
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    assert "off_market_execution" not in {a.rule for a in channel.sent}


def test_dated_recent_fill_is_still_price_checked(db, cipher, monkeypatch):
    """The path that already worked keeps working."""
    account_id, account_row = _settled_account(db, cipher, monkeypatch, label="undated-7")

    # A second past the settling poll, not "right now": at millisecond
    # resolution a fill stamped in the same millisecond as the previous poll is
    # a boundary case, and this test is about the ordinary recent fill rather
    # than that boundary.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 1000
    monkeypatch.setattr(
        "guardian.poller.make_client",
        lambda *a, **k: _client(fills=[_fill(timestamp=now_ms)], tickers=_ticker()),
    )
    channel = RecordingChannel()
    _poller(db, cipher, channel).run_fast_loop(account_row)

    assert "off_market_execution" in {a.rule for a in channel.sent}
