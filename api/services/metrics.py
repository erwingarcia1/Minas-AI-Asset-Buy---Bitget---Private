"""Prometheus metrics for the platform.

One registry, created here; /metrics in main.py exports it. Helpers wrap the
noisy label bookkeeping so services stay readable. Recording must never break
a request — the observe/inc helpers swallow their own errors.
"""
import logging
import time
from contextlib import contextmanager

from prometheus_client import (
    CollectorRegistry, Counter, Gauge, Histogram, generate_latest,
    CONTENT_TYPE_LATEST)

logger = logging.getLogger("autobuy.metrics")

registry = CollectorRegistry()

LATENCY_BUCKETS = (.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request latency",
    ["method", "path", "status"], registry=registry, buckets=LATENCY_BUCKETS)
http_requests_total = Counter(
    "http_requests_total", "HTTP requests served",
    ["method", "path", "status"], registry=registry)
active_purchases_total = Gauge(
    "active_purchases_total", "Enabled recurring purchases (set each scheduler tick)",
    registry=registry)
transactions_total = Counter(
    "transactions_total", "Transactions recorded",
    ["status", "crypto"], registry=registry)
binance_api_calls_total = Counter(
    "binance_api_calls_total", "Binance API calls",
    ["operation", "result"], registry=registry)
binance_api_duration_seconds = Histogram(
    "binance_api_duration_seconds", "Binance API call latency",
    ["operation"], registry=registry, buckets=LATENCY_BUCKETS)
email_send_duration_seconds = Histogram(
    "email_send_duration_seconds", "Email send latency",
    ["template"], registry=registry, buckets=LATENCY_BUCKETS)
emails_total = Counter(
    "emails_total", "Emails attempted", ["template", "result"], registry=registry)
scheduler_job_duration_seconds = Histogram(
    "scheduler_job_duration_seconds", "Scheduler job duration",
    ["job_type"], registry=registry, buckets=(.1, .5, 1, 5, 15, 30, 60, 120, 300))
scheduler_consecutive_failures = Gauge(
    "scheduler_consecutive_failures", "Consecutive failed purchase executions",
    registry=registry)


def get_registry() -> CollectorRegistry:
    return registry


def render_latest() -> tuple:
    """(payload bytes, content type) for the /metrics endpoint."""
    return generate_latest(registry), CONTENT_TYPE_LATEST


def record_http(method: str, path: str, status: int, duration_s: float) -> None:
    try:
        labels = (method, path, str(status))
        http_requests_total.labels(*labels).inc()
        http_request_duration_seconds.labels(*labels).observe(duration_s)
    except Exception:
        logger.exception("metric recording failed")


@contextmanager
def time_binance(operation: str):
    """Times a Binance call and counts its result."""
    start = time.perf_counter()
    try:
        yield
    except Exception:
        binance_api_calls_total.labels(operation, "error").inc()
        raise
    finally:
        binance_api_duration_seconds.labels(operation).observe(
            time.perf_counter() - start)
    binance_api_calls_total.labels(operation, "ok").inc()
