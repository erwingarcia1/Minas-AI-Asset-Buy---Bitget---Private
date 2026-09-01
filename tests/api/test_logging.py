import json
import logging

import httpx
import pytest

from api.services.logger import (
    RequestIdFilter, request_id_var, setup_logging)


@pytest.fixture(scope="module")
def boomy_app():
    from api.main import app
    @app.get("/test-only/boom")
    async def boom():
        raise RuntimeError("kaboom secret-detail")
    return app


async def test_unhandled_exception_returns_clean_500(boomy_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boomy_app, raise_app_exceptions=False),
                                 base_url="http://test") as c:
        r = await c.get("/test-only/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal server error"
    assert body["request_id"]
    assert "kaboom" not in r.text and "Traceback" not in r.text
    assert r.headers["x-request-id"] == body["request_id"]


async def test_request_id_propagates_from_header(boomy_app, caplog):
    with caplog.at_level(logging.INFO, logger="autobuy.access"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boomy_app),
                                     base_url="http://test") as c:
            r = await c.get("/health", headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["x-request-id"] == "trace-me-123"
    rec = next(rec for rec in caplog.records if rec.message == "request_completed")
    assert rec.request_id == "trace-me-123"
    assert rec.status_code == 200 and rec.duration_ms >= 0


async def test_error_responses_log_at_warning(boomy_app, caplog):
    with caplog.at_level(logging.INFO, logger="autobuy.access"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boomy_app),
                                     base_url="http://test") as c:
            await c.get("/api/user/profile")  # 401, no token
    rec = next(rec for rec in caplog.records if rec.message == "request_completed")
    assert rec.levelno == logging.WARNING and rec.status_code == 401


def test_json_formatter_output(capsys):
    setup_logging()
    root = logging.getLogger()
    handler = next(h for h in root.handlers if getattr(h, "_autobuy_json", False))
    token = request_id_var.set("req-42")
    try:
        record = logging.LogRecord("autobuy.test", logging.INFO, __file__, 1,
                                   "purchase_executed", None, None)
        record.user_id = "u-1"
        for f in handler.filters:
            f.filter(record)
        line = handler.format(record)
    finally:
        request_id_var.reset(token)
    parsed = json.loads(line)
    assert parsed["level"] == "INFO" and parsed["logger"] == "autobuy.test"
    assert parsed["message"] == "purchase_executed"
    assert parsed["request_id"] == "req-42" and parsed["user_id"] == "u-1"
    assert "timestamp" in parsed


def test_setup_logging_idempotent():
    setup_logging()
    setup_logging()
    root = logging.getLogger()
    assert sum(1 for h in root.handlers if getattr(h, "_autobuy_json", False)) == 1
