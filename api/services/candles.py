"""Historical prices for the portfolio chart: Bitget public candles, cached in
Redis per pair / granularity / page.

Cache policy: a page whose last bucket is still open (the newest page) lives
for one bucket; a page of fully closed buckets lives 24 hours. The per-user
series is never cached; it is computed from the user's rows plus these
pages, so every customer viewing the same range shares the same candle pages.
A bucket with no candle carries the previous close forward (a thin market or
a listing gap must never read as a price of zero); buckets before the pair's
first candle are None until the first close appears.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from api.services.cache import cached_json_sync
from api.services import bitget_market as bm
from api.services.bitget_market import MarketDataUnavailable

logger = logging.getLogger("autobuy.candles")

# range -> (bitget granularity, bucket seconds, number of buckets)
# Buckets are aligned to UTC: 15min/1h/4h candles open on UTC multiples of the
# step, and "1Dutc" days open at 00:00 UTC ("1day" would be Bitget's UTC+8 day,
# opening at 16:00 UTC; both verified live on 2026-09-22).
RANGES: Dict[str, tuple] = {
    "1D": ("15min", 15 * 60, 96),
    "7D": ("1h", 3600, 168),
    "30D": ("4h", 4 * 3600, 180),
    "90D": ("1Dutc", 86400, 90),
    "All": ("1Dutc", 86400, None),   # from the first buy; capped at ALL_MAX_DAYS
}
ALL_MAX_DAYS = 1000
PAGE_ROWS = 200          # rows per cache page (the history endpoint's maximum)
CLOSED_PAGE_TTL_S = 24 * 3600


def bucket_end(ts: float, step: int) -> int:
    """The end (exclusive start of the next bucket) of the bucket containing ts."""
    return (int(ts) // step + 1) * step


def fetch_page(symbol: str, granularity: str, end_ms: int, rows: int = PAGE_ROWS) -> List[list]:
    """`rows` candles whose close is at or before end_ms, oldest first. The newest
    page comes from the recent endpoint (no endTime needed); older pages from
    history-candles, whose endTime is inclusive on the candle's CLOSE time
    (verified live 2026-09-22: endTime=T returns the candle opening at T-step
    as its last row; endTime=T-1ms does not)."""
    client = bm._client()
    now_ms = int(time.time() * 1000)
    if end_ms >= now_ms - 60_000:
        data = client.get_candles(symbol, granularity, limit=min(rows, 1000))
    else:
        data = client.get_candles(symbol, granularity, limit=min(rows, 200), end_time_ms=end_ms, history=True)
    return sorted(data, key=lambda r: int(r[0]))


def _page_key(symbol: str, granularity: str, page_end_s: int) -> str:
    return f"bitget:candles:{symbol}:{granularity}:{page_end_s}"


def _cached_page(key: str, ttl: int, produce):
    """Fail closed like the rest of bitget_market: a producer failure with nothing
    cached is MarketDataUnavailable (-> 503). Tests replace this name."""
    def unavailable(exc: Exception):
        raise MarketDataUnavailable(f"Bitget candles unavailable for {key}: {exc}") from exc
    return cached_json_sync(key, ttl, produce, redis_factory=bm._sync_redis, on_produce_error=unavailable,
                            read_warning="candle cache read failed for %s", write_warning="candle cache write failed for %s")


def closes_for(symbol: str, granularity: str, step: int, bucket_ends: List[int]) -> Tuple[Dict[int, Optional[float]], List[int]]:
    """({bucket_end_s: close}, carried_bucket_ends) for every requested bucket
    end, oldest first. A bucket with no candle takes the previous bucket's
    close and is listed in carried_bucket_ends; buckets before the pair's
    first candle are None. Raises MarketDataUnavailable when Bitget cannot be
    reached and nothing is cached."""
    if not bucket_ends:
        return {}, []
    first, last = bucket_ends[0], bucket_ends[-1]
    # pages are aligned on PAGE_ROWS buckets so every viewer asks for the same keys
    page_span = PAGE_ROWS * step
    page_end = ((last - 1) // page_span + 1) * page_span
    page_ends = []
    while page_end > first - step:      # the page holding the open of the first bucket, too
        page_ends.append(page_end)
        page_end -= page_span
    now_s = int(time.time())
    by_open: Dict[int, float] = {}
    for page_end in sorted(page_ends):
        ttl = step if page_end > now_s else CLOSED_PAGE_TTL_S
        rows = _cached_page(_page_key(symbol, granularity, page_end), ttl,
                            lambda page_end=page_end: fetch_page(symbol, granularity, min(page_end, now_s) * 1000))
        for r in rows:
            by_open[int(r[0]) // 1000] = float(r[4])
    out: Dict[int, Optional[float]] = {}
    carried: List[int] = []
    # a gap at the start of the range carries the last close BEFORE the range
    # (the page usually holds it); with no earlier candle at all the bucket is None
    earlier = [o for o in by_open if o < first - step]
    prev: Optional[float] = by_open[max(earlier)] if earlier else None
    for end in bucket_ends:
        close = by_open.get(end - step)
        if close is None:
            close = prev          # gap: carry the previous close forward (None before the first candle)
            if close is not None:
                carried.append(end)
        out[end] = close
        prev = close
    return out, carried
