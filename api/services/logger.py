"""Structured JSON logging + request tracing for the API.

Every log line is a JSON object: {timestamp, level, logger, message, ...extras}.
A per-request id (contextvar) ties application logs to the request that caused
them and is returned to clients in the X-Request-ID header — 500 responses
carry it in the body so users can quote it to support.
"""
import contextvars
import logging
import sys
import time
import uuid

from pythonjsonlogger import jsonlogger

from api.config import settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


def setup_logging() -> None:
    """Route root logging through a JSON formatter. Idempotent."""
    root = logging.getLogger()
    if any(getattr(h, "_autobuy_json", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler._autobuy_json = True
    handler.setFormatter(jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"}))
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())


access_logger = logging.getLogger("autobuy.access")


async def request_logging_middleware(request, call_next):
    """Assign a request id, time the request, and log its completion."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = request_id_var.set(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    access_logger.log(level, "request_completed", extra={
        "method": request.method, "path": request.url.path,
        "status_code": response.status_code, "duration_ms": duration_ms,
        "request_id": request_id,
    })
    return response


async def unhandled_exception_handler(request, exc):
    """Log the traceback server-side; hand the client a bare 500 + request id."""
    from fastapi.responses import JSONResponse
    request_id = request_id_var.get()
    if request_id == "-":
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    logging.getLogger("autobuy.errors").exception(
        "unhandled_exception", extra={
            "path": request.url.path, "method": request.method,
            "request_id": request_id})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
        headers={"X-Request-ID": request_id})
