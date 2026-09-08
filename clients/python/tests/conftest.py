"""Mock-server fixtures: route handlers on top of httpx.MockTransport (no network)."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import httpx
import pytest

TOKENS = {"access_token": "acc-1", "refresh_token": "ref-1", "token_type": "bearer", "expires_in": 1800}


def json_response(data: Any, status: int = 200, headers: Dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers or {})


class MockServer:
    """Register (method, path) handlers; every call is logged for assertions."""

    def __init__(self) -> None:
        self.routes: Dict[tuple, Callable[[httpx.Request], httpx.Response]] = {}
        self.calls: List[httpx.Request] = []

    def on(self, method: str, path: str, handler: Callable[[httpx.Request], httpx.Response]) -> "MockServer":
        self.routes[(method.upper(), path)] = handler
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return json_response({"detail": "Not Found"}, 404)
        return handler(request)

    async def ahandle(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def async_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.ahandle)


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


@pytest.fixture
def server() -> MockServer:
    return MockServer()
