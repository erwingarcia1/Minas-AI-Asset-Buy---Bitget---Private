from __future__ import annotations

import threading
import time


class TokenBucket:
    """Standard token bucket. acquire() blocks until a token is available
    (or timeout). Thread-safe - shared across the worker pool."""

    def __init__(self, rate_per_sec: float, capacity: float):
        if rate_per_sec <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate_per_sec
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                deficit = tokens - self._tokens
                wait = deficit / self._rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(wait)


class RateLimiters:
    """One bucket per account credential (Bitget limits are per-UID) plus a
    single shared bucket for our egress IP (Bitget's 6000 req/min/IP global
    ceiling - docs/phase0-capability-audit.md section 4). Every REST call a
    worker makes must acquire from BOTH."""

    def __init__(self, per_account_rps: float, per_account_burst: float, ip_rps: float, ip_burst: float):
        self._per_account_rps = per_account_rps
        self._per_account_burst = per_account_burst
        self._account_buckets: dict[int, TokenBucket] = {}
        self._lock = threading.Lock()
        self.ip_bucket = TokenBucket(ip_rps, ip_burst)

    def account_bucket(self, account_id: int) -> TokenBucket:
        with self._lock:
            bucket = self._account_buckets.get(account_id)
            if bucket is None:
                bucket = TokenBucket(self._per_account_rps, self._per_account_burst)
                self._account_buckets[account_id] = bucket
            return bucket

    def acquire_for_call(self, account_id: int, calls: float = 1.0, timeout: float | None = 30.0) -> bool:
        if not self.account_bucket(account_id).acquire(calls, timeout=timeout):
            return False
        return self.ip_bucket.acquire(calls, timeout=timeout)
