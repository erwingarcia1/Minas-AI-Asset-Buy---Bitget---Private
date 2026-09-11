from datetime import datetime, timedelta, timezone

import httpx
import pytest

import api.services.market_data as md_mod
from tests.api.conftest import auth_headers, make_autobuy_purchase, make_history
from tests.api.test_analytics import FakeMarket


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def history(db, user, binance_key):
    year = datetime.now(timezone.utc).year
    when = datetime(year, 3, 15, 10, 30, tzinfo=timezone.utc)   # pinned into this calendar year
    btc = await make_autobuy_purchase(db, user, crypto="BTC", amount_usd=100.0, key=binance_key)
    eth = await make_autobuy_purchase(db, user, crypto="ETH", amount_usd=250.0, purchase_type="dca", key=binance_key)
    sol = await make_autobuy_purchase(db, user, crypto="SOL", amount_usd=990.0, key=binance_key)
    await make_history(db, user, purchase=btc, amount_crypto=0.002, cost_usd=100.0, fill_price=50000.0,
                       created_at=when, order_id="ORD-BTC-1", fee=0.1, fee_currency="USDT",
                       with_withdrawal=True, withdrawal_id="WD-1", withdrawal_fee=0.00005,
                       withdrawal_fee_currency="BTC", tx_hash="0xbtc1", withdrawal_chain="BTC")
    await make_history(db, user, purchase=btc, amount_crypto=0.001, cost_usd=60.0, fill_price=60000.0,
                       created_at=when + timedelta(days=1), order_id="ORD-BTC-2")
    await make_history(db, user, purchase=eth, amount_crypto=0.1, cost_usd=250.0, fill_price=2500.0,
                       created_at=when, order_id="ORD-ETH-1")
    await make_history(db, user, purchase=sol, amount_crypto=9.9, cost_usd=990.0, status="failed", created_at=when)
    return user


async def test_tax_summary_fifo_lots(db, history):
    year = datetime.now(timezone.utc).year
    async with client() as c:
        s = (await c.get("/api/reports/tax-summary", params={"year": year},
                         headers=auth_headers(history))).json()
    assert s["method"] == "FIFO" and s["realized_gains_usdt"] == 0.0
    btc = next(h for h in s["acquisitions"] if h["crypto"] == "BTC")
    assert btc["total_amount"] == pytest.approx(0.003)
    assert btc["total_cost_usdt"] == 160.0
    assert btc["average_price_usdt"] == pytest.approx(53333.33, abs=0.1)
    assert len(btc["lots"]) == 2
    assert s["total_acquired_usdt"] == 410.0  # failed tx excluded


async def test_tax_summary_wrong_year_empty(db, history):
    async with client() as c:
        s = (await c.get("/api/reports/tax-summary", params={"year": 2021},
                         headers=auth_headers(history))).json()
    assert s["acquisitions"] == [] and s["total_acquired_usdt"] == 0.0


async def test_tax_csv_export(db, history):
    year = datetime.now(timezone.utc).year
    async with client() as c:
        r = await c.get("/api/reports/tax-summary",
                        params={"year": year, "format": "csv"},
                        headers=auth_headers(history))
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == ("datetime_utc,crypto,quantity,fill_price_usd,usd_amount,fee,fee_currency,exchange,order_id,"
                        "purchase_type,tx_type,withdrawal_address,network,withdrawal_fee,withdrawal_fee_currency,"
                        "tx_hash,method")
    assert len(lines) == 4  # 3 successful buys; the failed one is excluded
    btc1 = next(l for l in lines if "ORD-BTC-1" in l)
    cells = btc1.split(",")
    assert cells[0].startswith(f"{year}-03-15 10:30:00") and cells[1] == "BTC" and cells[2] == "0.00200000"
    assert cells[3] == "50000.00" and cells[4] == "100.00" and cells[5] == "0.10000000" and cells[6] == "USDT"
    assert cells[7] == "binance" and cells[9] == "autobuy" and cells[10] == "withdrawal"
    assert cells[11] == "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq" and cells[12] == "BTC"
    assert cells[13] == "0.00005000" and cells[14] == "BTC" and cells[15] == "0xbtc1" and cells[16] == "FIFO"
    eth = next(l for l in lines if "ORD-ETH-1" in l).split(",")
    assert eth[10] == "buy" and eth[11] == "" and eth[12] == "", "held on exchange: no withdrawal columns"


async def test_pdf_export(db, history):
    year = datetime.now(timezone.utc).year
    async with client() as c:
        r = await c.get("/api/reports/export", params={"year": year},
                        headers=auth_headers(history))
        assert r.status_code == 200
        assert r.content.startswith(b"%PDF"), "must be a real PDF"
        assert (await c.get("/api/reports/export", params={"year": 2021},
                            headers=auth_headers(history))).status_code == 404


async def test_performance_metrics(db, history, monkeypatch):
    monkeypatch.setattr(md_mod, "market_data",
                        FakeMarket(prices={"BTC": 60000.0, "ETH": 3000.0}, vols={}))
    async with client() as c:
        p = (await c.get("/api/reports/performance",
                         headers=auth_headers(history))).json()
    assert p["has_data"] and p["price_available"]
    assert p["total_invested_usdt"] == 410.0
    # value: 0.003*60000 + 0.1*3000 = 480
    assert p["current_value_usdt"] == 480.0
    assert p["roi_pct"] == pytest.approx(17.07, abs=0.01)
    assert p["annualized_roi_pct"] > p["roi_pct"], "held under a year"
    assert p["purchase_count"] == 3


async def test_performance_no_data(db, user):
    async with client() as c:
        p = (await c.get("/api/reports/performance",
                         headers=auth_headers(user))).json()
    assert p == {"has_data": False}
