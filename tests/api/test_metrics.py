import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from api.services import metrics as m


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def sample_value(payload, name, labels):
    for family in text_string_to_metric_families(payload):
        for s in family.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


async def test_metrics_endpoint_prometheus_format():
    async with fresh_client() as c:
        await c.get("/")                       # generate one measurable request
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    families = {f.name for f in text_string_to_metric_families(r.text)}
    # counter families are exported without the _total suffix
    assert {"http_requests", "http_request_duration_seconds",
            "transactions", "binance_api_calls",
            "active_purchases_total", "scheduler_consecutive_failures"} <= families


async def test_http_metrics_increment_with_route_template():
    async with fresh_client() as c:
        before = sample_value((await c.get("/metrics")).text, "http_requests_total",
                              {"path": "/", "method": "GET", "status": "200"}) or 0
        await c.get("/")
        after = sample_value((await c.get("/metrics")).text, "http_requests_total",
                             {"path": "/", "method": "GET", "status": "200"})
    assert after == before + 1


async def test_metrics_and_health_not_self_counted():
    async with fresh_client() as c:
        for _ in range(3):
            await c.get("/metrics")
            await c.get("/health")
        payload = (await c.get("/metrics")).text
    assert sample_value(payload, "http_requests_total",
                        {"path": "/metrics"}) is None
    assert sample_value(payload, "http_requests_total",
                        {"path": "/health"}) is None


async def test_transaction_metric_recorded(db, user, purchase, binance_key, monkeypatch):
    from api.services import purchase_service as ps_mod
    from api.services.purchase_service import PurchaseService
    from tests.api.test_purchase_service import FakeBinance
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    before = m.transactions_total.labels("completed", "BTC")._value.get()
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"
    assert m.transactions_total.labels("completed", "BTC")._value.get() == before + 1


def test_binance_call_timer_counts_errors():
    before_ok = m.binance_api_calls_total.labels("get_price", "ok")._value.get()
    before_err = m.binance_api_calls_total.labels("get_price", "error")._value.get()
    with m.time_binance("get_price"):
        pass
    with pytest.raises(ValueError):
        with m.time_binance("get_price"):
            raise ValueError("boom")
    assert m.binance_api_calls_total.labels("get_price", "ok")._value.get() == before_ok + 1
    assert m.binance_api_calls_total.labels("get_price", "error")._value.get() == before_err + 1


async def test_consecutive_failure_alert_fires(db, user, purchase, binance_key, monkeypatch, caplog):
    """Simulated outage: repeated failures raise the gauge and emit CRITICAL."""
    import logging
    from api.services import purchase_service as ps_mod
    from api.services.recurring_scheduler import RecurringScheduler
    from tests.api.test_purchase_service import FailingBinance
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(ps_mod, "BinanceClient", FailingBinance)
    sched = RecurringScheduler(dry_run=True)
    with caplog.at_level(logging.CRITICAL, logger="autobuy.scheduler"):
        for _ in range(5):
            purchase.next_purchase_date = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.commit()
            assert await sched.tick() == 1
    assert sched.consecutive_failures == 5
    assert m.scheduler_consecutive_failures._value.get() == 5
    assert any("ALERT" in rec.message for rec in caplog.records), \
        "5 consecutive failures must emit a CRITICAL alert log"
