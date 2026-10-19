from __future__ import annotations

import threading
import time


class CircuitBreaker:
    """Per-account breaker: after `failure_threshold` consecutive failures
    the account is skipped for an exponentially growing backoff window
    (base * 2^trips, capped at max). One bad key or banned IP must never
    stall the fleet - the worker just sees allow() == False and moves on.
    Success fully resets both the count and the backoff exponent."""

    def __init__(self, failure_threshold: int, base_backoff_seconds: float, max_backoff_seconds: float):
        self._threshold = failure_threshold
        self._base = base_backoff_seconds
        self._max = max_backoff_seconds
        self._lock = threading.Lock()
        self._failures = 0
        self._trips = 0
        self._open_until: float | None = None

    def allow(self) -> bool:
        with self._lock:
            if self._open_until is None:
                return True
            if time.monotonic() >= self._open_until:
                # Half-open: allow one trial call through.
                self._open_until = None
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._trips = 0
            self._open_until = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                backoff = min(self._base * (2**self._trips), self._max)
                self._open_until = time.monotonic() + backoff
                self._trips += 1
                self._failures = 0

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open_until is not None and time.monotonic() < self._open_until


class BreakerRegistry:
    def __init__(self, failure_threshold: int, base_backoff_seconds: float, max_backoff_seconds: float):
        self._args = (failure_threshold, base_backoff_seconds, max_backoff_seconds)
        self._breakers: dict[int, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def for_account(self, account_id: int) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(account_id)
            if breaker is None:
                breaker = CircuitBreaker(*self._args)
                self._breakers[account_id] = breaker
            return breaker
