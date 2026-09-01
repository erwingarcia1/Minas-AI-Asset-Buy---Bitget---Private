from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import update

import api.services.market_data as md_mod
from api.models import Transaction
from tests.api.conftest import auth_headers
from tests.api.test_analytics import FakeMarket


def client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def history(db, user):
    txs = [
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.002,
                    cost_usdt=100.0, exchange_rate=50000.0, status="completed"),
        Transaction(user_id=user.id, crypto="BTC", amount_crypto=0.001,
                    cost_usdt=60.0, exchange_rate=60000.0, status="completed"),
        Transaction(user_id=user.id, crypto="ETH", amount_crypto=0.1,
                    cost_usdt=250.0, exchange_rate=2500.0, status="completed"),
        Transaction(user_id=user.id, crypto="SOL", amount_crypto=9.9,
                    cost_usdt=990.0, status="failed"),
    ]
    db.add_all(txs)
    await db.commit()
    # pin all rows into this calendar year deterministically
    await db.execute(update(Transaction).where(Transaction.user_id == user.id)
                     .values(created_at=datetime(datetime.now(timezone.utc).year, 3, 15,
                                                 tzinfo=timezone.utc)))
    await db.commit()
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
    assert lines[0] == "crypto,acquired_date,amount,cost_usdt,price_usdt,method"
    assert len(lines) == 4  # 3 completed lots
    assert any("BTC" in l and "50000.00" in l for l in lines)


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
