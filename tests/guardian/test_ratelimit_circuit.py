from __future__ import annotations

import time

from guardian.circuit import BreakerRegistry, CircuitBreaker
from guardian.ratelimit import RateLimiters, TokenBucket


def test_token_bucket_allows_burst_then_throttles():
    bucket = TokenBucket(rate_per_sec=100, capacity=2)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False  # burst spent, refill hasn't caught up


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(rate_per_sec=50, capacity=1)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    time.sleep(0.05)  # 50/s -> ~2.5 tokens accrued, capped at capacity 1
    assert bucket.try_acquire() is True


def test_acquire_times_out_instead_of_hanging():
    bucket = TokenBucket(rate_per_sec=0.001, capacity=1)
    assert bucket.acquire() is True
    assert bucket.acquire(timeout=0.05) is False


def test_rate_limiters_isolate_accounts_but_share_ip():
    limiters = RateLimiters(per_account_rps=100, per_account_burst=1, ip_rps=100, ip_burst=2)
    assert limiters.acquire_for_call(1, timeout=0.01) is True
    assert limiters.acquire_for_call(2, timeout=0.01) is True
    # Both account buckets have capacity again quickly, but the shared IP
    # bucket (burst 2) is now empty - the third call must wait/timeout.
    assert limiters.ip_bucket.try_acquire() is False


def test_breaker_opens_after_threshold_and_recovers():
    breaker = CircuitBreaker(failure_threshold=2, base_backoff_seconds=0.05, max_backoff_seconds=1)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is True  # below threshold
    breaker.record_failure()
    assert breaker.is_open is True
    assert breaker.allow() is False  # open: account skipped, fleet moves on
    time.sleep(0.06)
    assert breaker.allow() is True  # half-open trial
    breaker.record_success()
    assert breaker.is_open is False


def test_breaker_backoff_doubles_on_consecutive_trips():
    breaker = CircuitBreaker(failure_threshold=1, base_backoff_seconds=0.05, max_backoff_seconds=10)
    breaker.record_failure()  # trip 1: 0.05s
    time.sleep(0.06)
    assert breaker.allow() is True
    breaker.record_failure()  # trip 2: 0.1s
    time.sleep(0.06)
    assert breaker.allow() is False  # still open - backoff doubled


def test_registry_returns_same_breaker_per_account():
    registry = BreakerRegistry(3, 1, 10)
    assert registry.for_account(7) is registry.for_account(7)
    assert registry.for_account(7) is not registry.for_account(8)
