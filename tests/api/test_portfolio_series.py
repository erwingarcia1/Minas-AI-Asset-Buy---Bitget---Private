"""Portfolio chart series: candle gaps, bucket math, the series builder and the
read-only route (GET /api/dashboard/portfolio/series)."""
from datetime import datetime, timedelta, timezone

import pytest

import api.services.market_data as md_mod
from api.services import candles
from api.services import portfolio_series as ps
from api.services.bitget_market import MarketDataUnavailable
from tests.api.conftest import auth_headers, make_autobuy_purchase, make_history

STEP = 3600
T0 = 1_700_000_000 - 1_700_000_000 % (candles.PAGE_ROWS * STEP)   # page-aligned base, well in the past


def candle(open_s, close):
    return [str(open_s * 1000), "1", "1", "1", str(close), "0", "0", "0"]


class FakeMarket:
    def __init__(self, prices):
        self.prices = prices
    async def get_price(self, crypto):
        return self.prices.get(crypto.upper())


# ---- candles.closes_for -------------------------------------------------------

def test_gap_in_the_middle_carries_the_previous_close_forward(monkeypatch):
    opens = {T0: 10.0, T0 + STEP: 11.0, T0 + 2 * STEP: 12.0, T0 + 4 * STEP: 14.0}   # bucket opening at 3*STEP is missing
    monkeypatch.setattr(candles, "fetch_page", lambda *a, **k: [candle(o, c) for o, c in opens.items()])
    ends = [T0 + (i + 1) * STEP for i in range(5)]
    closes, carried = candles.closes_for("XLMUSDT", "1h", STEP, ends)
    assert [closes[e] for e in ends] == [10.0, 11.0, 12.0, 12.0, 14.0]
    assert carried == [T0 + 4 * STEP], "the bucket with no candle is priced at the previous close and reported"


def test_gap_at_the_start_carries_the_last_close_before_the_range(monkeypatch):
    opens = {T0: 9.0, T0 + 3 * STEP: 13.0}    # range starts at the bucket opening at 2*STEP; 2*STEP has no candle
    monkeypatch.setattr(candles, "fetch_page", lambda *a, **k: [candle(o, c) for o, c in opens.items()])
    ends = [T0 + 3 * STEP, T0 + 4 * STEP]
    closes, carried = candles.closes_for("XLMUSDT", "1h", STEP, ends)
    assert [closes[e] for e in ends] == [9.0, 13.0]
    assert carried == [T0 + 3 * STEP]


def test_no_candle_at_all_before_the_first_bucket_is_none(monkeypatch):
    opens = {T0 + 3 * STEP: 13.0}
    monkeypatch.setattr(candles, "fetch_page", lambda *a, **k: [candle(o, c) for o, c in opens.items()])
    ends = [T0 + 3 * STEP, T0 + 4 * STEP]
    closes, carried = candles.closes_for("NEWUSDT", "1h", STEP, ends)
    assert [closes[e] for e in ends] == [None, 13.0] and carried == []


def test_pages_are_aligned_and_cover_the_first_bucket(monkeypatch):
    asked = []
    monkeypatch.setattr(candles, "fetch_page", lambda symbol, gran, end_ms, rows=200: asked.append(end_ms // 1000) or [])
    span = candles.PAGE_ROWS * STEP
    # first bucket end exactly on a page boundary: its open lives in the page ending there
    candles.closes_for("BTCUSDT", "1h", STEP, [T0 + span, T0 + span + STEP])
    assert asked == [T0 + span, T0 + 2 * span]


def test_unreachable_bitget_is_market_data_unavailable(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("no route")
    monkeypatch.setattr(candles, "fetch_page", boom)
    with pytest.raises(MarketDataUnavailable):
        candles.closes_for("BTCUSDT", "1h", STEP, [T0 + STEP])


# ---- bucket math --------------------------------------------------------------

def test_bucket_ends_per_range():
    now = T0 + 5 * 86400 + 1234
    for key, (gran, step, count) in candles.RANGES.items():
        if count is None:
            continue
        g, s, ends = ps.bucket_ends_for(key, now, None)
        assert (g, s, len(ends)) == (gran, step, count)
        assert ends[-1] == candles.bucket_end(now, step) - step, "last point is the last CLOSED bucket"
        assert all(b - a == step for a, b in zip(ends, ends[1:]))
    g, s, ends = ps.bucket_ends_for("All", now, first_buy=now - 3 * 86400 - 10)
    assert g == "1Dutc" and len(ends) == 4, "All runs from the UTC day of the first buy"
    assert all(e % 86400 == 0 for e in ends), "day buckets end at 00:00 UTC (Bitget 1Dutc), not Bitget's UTC+8 day"
    assert ps.bucket_ends_for("All", now, None)[2] == []


# ---- the series builder, on real rows ----------------------------------------

@pytest.fixture
async def two_buys(db, user, binance_key):
    now = datetime.now(timezone.utc)
    btc = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key,
                                      status="completed", frequency="one_time", next_execution=None)
    await make_history(db, user, purchase=btc, amount_crypto=0.002, cost_usd=100.0,
                       created_at=now - timedelta(hours=10), fee=0.000002, fee_currency="BTC")   # net 0.001998
    xlm = await make_autobuy_purchase(db, user, crypto="XLM", amount_usd=20.0, key=binance_key,
                                      status="completed", frequency="one_time", next_execution=None)
    await make_history(db, user, purchase=xlm, amount_crypto=50.0, cost_usd=20.0, created_at=now - timedelta(hours=3))
    await make_history(db, user, purchase=xlm, amount_crypto=1.0, cost_usd=999.0, status="failed",
                       created_at=now - timedelta(hours=2))
    return user, now


@pytest.fixture
def flat_candles(monkeypatch):
    """Every bucket closes at BTC 50000 / XLM 0.4."""
    def fetch_page(symbol, gran, end_ms, rows=200):
        step = {"15min": 900, "1h": 3600, "4h": 14400, "1Dutc": 86400}[gran]
        close = {"BTCUSDT": 50000.0, "XLMUSDT": 0.4}[symbol]
        last_open = (end_ms // 1000 // step) * step      # like Bitget: bucket-aligned opens, the newest bucket still open
        return [candle(last_open - i * step, close) for i in range(rows)][::-1]
    monkeypatch.setattr(candles, "fetch_page", fetch_page)


async def test_series_steps_and_final_point(db, two_buys, flat_candles, monkeypatch):
    user, now = two_buys
    monkeypatch.setattr(md_mod, "market_data", FakeMarket({"BTC": 60000.0, "XLM": 0.5}))
    s = await ps.build(db, user.id, "7D", now=now.timestamp())
    assert s.granularity == "1h" and len(s.points) == 168 + 1
    invested = [p["invested"] for p in s.points]
    assert invested[0] == 0.0 and invested[-1] == 120.0 and sorted(invested) == invested, "invested only steps up"
    assert 100.0 in invested, "the BTC buy alone is a step before the XLM buy lands"
    before = next(p for p in s.points if p["invested"] == 100.0)
    assert before["value"] == round(0.001998 * 50000.0, 2), "net of the BTC-denominated fee, at the candle close"
    assert s.points[-2]["value"] == round(0.001998 * 50000.0 + 50.0 * 0.4, 2)
    assert s.points[-1]["value"] == s.current_value == round(0.001998 * 60000.0 + 50.0 * 0.5, 2), "final point at the current price"
    assert s.points[-1]["t"] == int(now.timestamp()) and s.invested == 120.0
    assert [b["crypto"] for b in s.buys] == ["BTC", "XLM"] and s.buys[1]["cost_usd"] == 20.0, "failed buys are not markers"
    # change excludes the 120 USDT the two buys added: (26.2 total) 0.001998*60000 + 25 = 144.88 - 0 - 120
    assert s.change_usdt == round(s.current_value - 120.0, 2)
    assert s.change_pct == round(s.change_usdt / 120.0 * 100, 2)
    assert s.carried_forward == 0 and s.unpriced == []


async def test_series_without_current_price_has_no_final_point(db, two_buys, flat_candles, monkeypatch):
    user, now = two_buys
    monkeypatch.setattr(md_mod, "market_data", FakeMarket({}))
    s = await ps.build(db, user.id, "1D", now=now.timestamp())
    assert s.current_value is None and s.change_usdt is None and len(s.points) == 96
    assert s.unpriced == ["BTC", "XLM"]


async def test_series_empty_user(db, user):
    s = await ps.build(db, user.id, "All")
    assert s.points == [] and s.buys == [] and s.current_value is None


# ---- the route ----------------------------------------------------------------

async def test_route_matches_performance_market_value(client, two_buys, flat_candles, monkeypatch):
    user, _ = two_buys
    monkeypatch.setattr(md_mod, "market_data", FakeMarket({"BTC": 60000.0, "XLM": 0.5}))
    async with client as c:
        series = (await c.get("/api/dashboard/portfolio/series?range=30D", headers=auth_headers(user))).json()
        perf = (await c.get("/api/analytics/performance", headers=auth_headers(user))).json()
    assert series["range"] == "30D" and series["granularity"] == "4h"
    assert series["current_value"] == 0.001998 * 60000.0 + 25.0 == 144.88
    assert series["current_value"] == perf["total_market_value"], "chart final point == performance market value"
    assert series["points"][-1]["value"] == series["current_value"]
    assert "not financial advice" in series["note"]


async def test_route_rejects_unknown_range(client, user):
    async with client as c:
        r = await c.get("/api/dashboard/portfolio/series?range=2W", headers=auth_headers(user))
    assert r.status_code == 422


async def test_route_503_when_candles_unavailable(client, two_buys):
    user, _ = two_buys        # conftest's fetch_page stub raises: Bitget "unreachable", nothing cached
    async with client as c:
        r = await c.get("/api/dashboard/portfolio/series?range=7D", headers=auth_headers(user))
        d = await c.get("/api/dashboard/", headers=auth_headers(user))
    assert r.status_code == 503 and r.json()["detail"]["error"] == "exchange_data_unavailable"
    assert d.status_code == 200 and {h["crypto"] for h in d.json()["holdings"]} == {"BTC", "XLM"}, "the rest of the dashboard is unaffected"


async def test_route_is_self_scoped(client, db, two_buys, flat_candles, monkeypatch):
    from api.models import User
    other = User(email="other-series@example.com", password_hash="x", kyc_status="verified")
    db.add(other)
    await db.commit()
    monkeypatch.setattr(md_mod, "market_data", FakeMarket({"BTC": 60000.0, "XLM": 0.5}))
    async with client as c:
        r = (await c.get("/api/dashboard/portfolio/series?range=7D", headers=auth_headers(other))).json()
    assert r["points"] == [] and r["buys"] == [] and r["invested"] == 0.0
    await db.delete(other)
    await db.commit()
