"""G-48 (#52): an operator is paged when an exchange stops stamping times.

The counter has existed since 163021e and its resting value is exactly zero, so
the threshold is derived rather than chosen: any increase at all is anomalous.
What was missing was something watching it and somewhere for the page to go —
the latter arrived with #53.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from guardian.alarms import UntimestampedRecordAlarm
from guardian.alerts.operator import OperatorAlerter, recent_operator_pages
from guardian.config import RulesConfig
from guardian.metrics import Metrics
from tests.guardian.conftest import SHIPPED_RULES


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


@pytest.fixture
def channels():
    return {"CRITICAL": [Recording()], "HIGH": [Recording()], "MEDIUM": [Recording()], "LOW": [Recording()]}


@pytest.fixture
def alerter(db, channels):
    return OperatorAlerter(db, channels, RulesConfig(SHIPPED_RULES))


@pytest.fixture
def metrics():
    from prometheus_client import CollectorRegistry

    return Metrics(registry=CollectorRegistry())


def _alarm(metrics, alerter, window=60, consecutive=2):
    return UntimestampedRecordAlarm(
        metrics, alerter, window_seconds=window, consecutive_windows=consecutive
    )


def test_a_quiet_counter_never_pages(metrics, alerter, channels):
    """Resting value is zero. Zero must stay silent, or the alarm trains its
    operator to ignore it."""
    alarm = _alarm(metrics, alerter)
    for t in range(0, 601, 60):
        alarm.evaluate(now=t)

    assert channels["HIGH"][0].sent == []


def test_a_single_stray_record_does_not_page(metrics, alerter, channels):
    """One undated record in one window is not a format change. The issue asks
    for the rise to be *sustained* — held across evaluation periods — so a
    transient doesn't wake anyone."""
    alarm = _alarm(metrics, alerter, consecutive=2)
    alarm.evaluate(now=0)

    metrics.untimestamped_record("bitget", "fill")
    alarm.evaluate(now=60)   # first non-zero window
    alarm.evaluate(now=120)  # quiet again

    assert channels["HIGH"][0].sent == []


def test_a_sustained_rise_pages_an_operator(metrics, alerter, channels):
    """AC 1: without a human watching a graph."""
    alarm = _alarm(metrics, alerter, consecutive=2)
    alarm.evaluate(now=0)

    metrics.untimestamped_record("bitget", "fill")
    alarm.evaluate(now=60)
    metrics.untimestamped_record("bitget", "fill")
    alarm.evaluate(now=120)

    assert len(channels["HIGH"][0].sent) == 1


def test_the_page_names_the_exchange_and_the_record_kind(metrics, alerter, channels):
    """AC 2: triage starts from "Binance changed its fills response", not
    "something is wrong"."""
    alarm = _alarm(metrics, alerter, consecutive=2)
    alarm.evaluate(now=0)
    for t in (60, 120):
        metrics.untimestamped_record("binance", "fill")
        alarm.evaluate(now=t)

    page = channels["HIGH"][0].sent[0]
    assert "binance" in page.message
    assert "fill" in page.message
    assert page.evidence["exchange"] == "binance"
    assert page.evidence["source"] == "fill"


def test_each_exchange_and_kind_is_tracked_separately(metrics, alerter, channels):
    """Bitget dropping timestamps on withdrawals and Binance dropping them on
    fills are two different investigations."""
    alarm = _alarm(metrics, alerter, consecutive=2)
    alarm.evaluate(now=0)
    for t in (60, 120):
        metrics.untimestamped_record("binance", "fill")
        metrics.untimestamped_record("bitget", "withdrawal")
        alarm.evaluate(now=t)

    rules = {(p.evidence["exchange"], p.evidence["source"]) for p in channels["HIGH"][0].sent}
    assert rules == {("binance", "fill"), ("bitget", "withdrawal")}


def test_a_healthy_window_resets_the_streak(metrics, alerter, channels):
    """A format change is continuous. An intermittent record that clears must
    not accumulate towards a page across unrelated hours."""
    alarm = _alarm(metrics, alerter, consecutive=2)
    alarm.evaluate(now=0)

    metrics.untimestamped_record("bitget", "fill")
    alarm.evaluate(now=60)
    alarm.evaluate(now=120)  # clean window - streak resets
    metrics.untimestamped_record("bitget", "fill")
    alarm.evaluate(now=180)

    assert channels["HIGH"][0].sent == []


def test_the_window_is_respected(metrics, alerter, channels):
    """Evaluating more often than the window must not shorten it — otherwise
    "two evaluation periods" becomes "two scheduler ticks", which is 90 seconds
    rather than the half hour intended."""
    alarm = _alarm(metrics, alerter, window=900, consecutive=2)
    alarm.evaluate(now=0)

    # Records arriving continuously, the way a real format change looks. Each
    # scheduler tick would otherwise see a fresh non-zero delta, so two ticks -
    # 90 seconds - would satisfy "two evaluation periods" and page.
    for t in range(45, 900, 45):
        metrics.untimestamped_record("bitget", "fill")
        alarm.evaluate(now=t)

    assert channels["HIGH"][0].sent == [], (
        "paged inside one window - the window is being measured in scheduler "
        "ticks rather than in seconds"
    )

    # And it does page once the window has actually passed twice.
    for t in (900, 1800):
        metrics.untimestamped_record("bitget", "fill")
        alarm.evaluate(now=t)
    assert len(channels["HIGH"][0].sent) == 1


def test_the_page_is_recorded_and_stays_off_the_customers_history(db, metrics, alerter):
    """AC 4's reuse point, and #53's guarantee: this is our fault, and it goes
    in our table."""
    alarm = _alarm(metrics, alerter, consecutive=2)
    alarm.evaluate(now=0)
    for t in (60, 120):
        metrics.untimestamped_record("bitget", "fill")
        alarm.evaluate(now=t)

    pages = recent_operator_pages(db)
    assert len(pages) == 1
    assert pages[0]["account_id"] is None
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_alerts")
        assert cur.fetchone()["c"] == 0


def test_the_alarm_works_without_prometheus_installed(db, alerter, channels):
    """The counter is a Prometheus metric, but the alarm must not depend on
    prometheus_client being importable — `Metrics` is explicitly a no-op shell
    when it isn't, and an alarm that quietly stops existing with an optional
    dependency is worse than no alarm."""
    import guardian.metrics as metrics_module

    was = metrics_module._AVAILABLE
    metrics_module._AVAILABLE = False
    try:
        metrics = Metrics()
        alarm = _alarm(metrics, alerter, consecutive=2)
        alarm.evaluate(now=0)
        for t in (60, 120):
            metrics.untimestamped_record("bitget", "fill")
            alarm.evaluate(now=t)
    finally:
        metrics_module._AVAILABLE = was

    assert len(channels["HIGH"][0].sent) == 1


def test_the_alarm_is_wired_into_the_engine(db, cipher):
    """AC 3: the path is exercised, not assumed. An alarm that exists but was
    never connected is the same as no alarm."""
    from prometheus_client import CollectorRegistry

    from guardian.engine import Engine

    metrics = Metrics(registry=CollectorRegistry())
    engine = Engine(
        db, cipher, RulesConfig(SHIPPED_RULES), {},
        metrics=metrics,
        operator_alerter=OperatorAlerter(db, {}, RulesConfig(SHIPPED_RULES)),
    )
    assert engine._untimestamped_alarm is not None

    # Drive it the way the scheduler does, and require a page to land.
    # Driven at the window the shipped config actually configures, so this
    # fails if that default silently changes to something an operator would
    # never reach.
    engine._untimestamped_alarm.evaluate(now=0)
    for t in (900, 1800):
        metrics.untimestamped_record("bitget", "fill")
        engine._untimestamped_alarm.evaluate(now=t)

    assert len(recent_operator_pages(db)) == 1


def test_the_scheduler_actually_evaluates_the_alarm(db, cipher):
    """Wiring, separate from behaviour.

    Every other test here drives `evaluate` directly, so all of them keep
    passing if the call is dropped from the scheduler - which is precisely the
    "exists but was never connected" failure this issue set as its own
    standard. The behaviour is covered above; this pins that something calls
    it."""
    from guardian.alerts.operator import OperatorAlerter
    from guardian.engine import Engine

    class Spy:
        def __init__(self):
            self.calls = 0

        def evaluate(self, now):
            self.calls += 1

    engine = Engine(
        db, cipher, RulesConfig(SHIPPED_RULES), {},
        metrics=Metrics(registry=CollectorRegistry()),
        operator_alerter=OperatorAlerter(db, {}, RulesConfig(SHIPPED_RULES)),
    )
    spy = Spy()
    engine._untimestamped_alarm = spy
    engine._scheduler_tick()

    assert spy.calls == 1, "the scheduler is not evaluating the untimestamped-record alarm"
