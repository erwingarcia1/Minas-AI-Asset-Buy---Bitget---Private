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
            "transactions", "exchange_api_calls",
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


async def test_transaction_metric_recorded(db, user, binance_key):
    from datetime import timedelta
    from api.models import utcnow
    from api.tasks import purchase_scheduler as ps
    from tests.api.conftest import make_autobuy_purchase
    from tests.test_purchase_events import RichGateway
    await make_autobuy_purchase(db, user, key=binance_key, next_execution=utcnow() - timedelta(minutes=1))
    before = m.transactions_total.labels("completed", "BTC")._value.get()
    counts = await ps.execute_due_purchases(db, gateway=RichGateway(), dry_run=True)
    assert counts["executed"] == 1
    assert m.transactions_total.labels("completed", "BTC")._value.get() == before + 1
    assert m.active_purchases_total._value.get() >= 1


def test_binance_call_timer_counts_errors():
    before_ok = m.exchange_api_calls_total.labels("get_price", "ok")._value.get()
    before_err = m.exchange_api_calls_total.labels("get_price", "error")._value.get()
    with m.time_exchange("get_price"):
        pass
    with pytest.raises(ValueError):
        with m.time_exchange("get_price"):
            raise ValueError("boom")
    assert m.exchange_api_calls_total.labels("get_price", "ok")._value.get() == before_ok + 1
    assert m.exchange_api_calls_total.labels("get_price", "error")._value.get() == before_err + 1


async def test_consecutive_failure_alert_fires(db, user, binance_key, monkeypatch, caplog):
    """Simulated outage: five Auto-Buy runs that fail every due purchase raise
    the gauge to 5, emit CRITICAL and page Slack."""
    import logging
    from datetime import timedelta
    from api.models import utcnow
    from api.services import scheduler_state, slack_service
    from api.tasks import purchase_scheduler as ps
    from tests.api.conftest import make_autobuy_purchase
    from tests.test_purchase_events import BuyFails
    await scheduler_state.bump_failures("autobuy", ok=True)   # start from 0
    slack = []

    async def fake_slack(text):
        slack.append(text)
        return True
    monkeypatch.setattr(slack_service, "send_slack", fake_slack)
    p = await make_autobuy_purchase(db, user, key=binance_key, next_execution=utcnow() - timedelta(minutes=1))
    with caplog.at_level(logging.CRITICAL, logger="autobuy.tasks.purchases"):
        for _ in range(5):
            p.status, p.failure_count, p.next_execution = "active", 0, utcnow() - timedelta(minutes=1)
            await db.commit()
            counts = await ps.execute_due_purchases(db, gateway=BuyFails(), dry_run=True)
            assert counts["failed"] == 1
    assert await scheduler_state.consecutive_failures("autobuy") == 5
    assert m.scheduler_consecutive_failures._value.get() == 5
    assert any("ALERT" in rec.message for rec in caplog.records), \
        "5 consecutive failures must emit a CRITICAL alert log"
    assert any("5 consecutive" in t for t in slack)
    await scheduler_state.bump_failures("autobuy", ok=True)
