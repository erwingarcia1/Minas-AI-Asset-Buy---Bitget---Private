import httpx
import pytest


def fresh_client():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_swagger_ui_loads():
    async with fresh_client() as c:
        r = await c.get("/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text


async def test_redoc_loads():
    async with fresh_client() as c:
        r = await c.get("/redoc")
    assert r.status_code == 200 and "redoc" in r.text.lower()


async def test_openapi_schema_valid_and_complete():
    async with fresh_client() as c:
        spec = (await c.get("/openapi.json")).json()
    assert spec["openapi"].startswith("3.")
    info = spec["info"]
    assert info["title"] == "AutoBuy Crypto API" and info["version"] == "0.2.0"
    from api.config import settings
    assert info["contact"].get("email", "") == settings.support_email
    assert {s["url"].rstrip("/") for s in spec["servers"]} == {
        "http://localhost:8000", "https://api.autobuy.app"}
    # every operation must carry a real description (from its docstring)
    undocumented = [
        f"{method.upper()} {path}"
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if method in ("get", "post", "put", "delete", "patch")
        and not path.startswith("/test-only")   # injected by the logging tests
        and not (op.get("description") or op.get("summary"))
    ]
    assert undocumented == [], f"undocumented operations: {undocumented}"
    # sanity: the whole surface is present
    assert len(spec["paths"]) >= 30


async def test_all_router_endpoints_have_docstrings():
    from api.main import app
    from fastapi.routing import APIRoute
    missing = [r.path for r in app.routes
               if isinstance(r, APIRoute) and not (r.endpoint.__doc__ or "").strip()
               and r.path != "/metrics" and not r.path.startswith("/test-only")]
    assert missing == [], f"routes lacking docstrings: {missing}"
