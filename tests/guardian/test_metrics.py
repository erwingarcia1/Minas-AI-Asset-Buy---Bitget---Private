from __future__ import annotations

from prometheus_client import CollectorRegistry

from guardian.metrics import Metrics


def _sample(registry, name, **labels):
    value = registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_untimestamped_records_counter_is_exported_and_labelled():
    """The counter has to actually reach the Prometheus registry under a
    stable name and label set - that name is what an alarm rule binds to, so
    renaming it silently breaks the page it exists to trigger."""
    registry = CollectorRegistry()
    metrics = Metrics(registry=registry)

    metrics.untimestamped_record("binance-main", "fill")
    metrics.untimestamped_record("binance-main", "fill")
    metrics.untimestamped_record("binance-main", "withdrawal")

    name = "guardian_untimestamped_records_total"
    assert _sample(registry, name, exchange="binance-main", source="fill") == 2.0
    assert _sample(registry, name, exchange="binance-main", source="withdrawal") == 1.0
    # An exchange that has never misbehaved must not appear as a zero series
    # that dilutes a rate() across the whole fleet.
    assert registry.get_sample_value(name, {"exchange": "bitget-main", "source": "fill"}) is None


def test_metrics_never_touch_the_global_registry():
    """The worker serves its own registry (v0.9.1): the API has its metrics on
    the process-wide one, and two Metrics() in one process must not collide."""
    from prometheus_client import REGISTRY

    first, second = Metrics(), Metrics()
    first.untimestamped_record("x", "fill")
    assert REGISTRY.get_sample_value("guardian_untimestamped_records_total", {"exchange": "x", "source": "fill"}) is None
    assert first.registry is not second.registry
    assert b"guardian_untimestamped_records_total" in first.exposition()[1]
