from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from api.config import enforce_production_secrets, settings
from api.database import init_db, close_db, get_db
from api.services.logger import (
    platform_middleware, setup_logging, unhandled_exception_handler)

setup_logging()

# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    enforce_production_secrets(settings)
    await init_db()
    print("✓ Database initialized")
    if settings.scheduler_enabled:
        from api.services.recurring_scheduler import scheduler
        scheduler.poll_seconds = settings.scheduler_poll_seconds
        scheduler.dry_run = settings.autobuy_dry_run
        scheduler.start()
        print(f"✓ Scheduler started (dry_run={settings.autobuy_dry_run})")
    yield
    # Shutdown
    if settings.scheduler_enabled:
        from api.services.recurring_scheduler import scheduler
        scheduler.stop()
    await close_db()
    print("✓ Database closed")

OPENAPI_TAGS = [
    {"name": "auth", "description": "Registration, login, JWT lifecycle (refresh, logout, password change)."},
    {"name": "user", "description": "Profile management. `kyc_status` is read-only here — it is owned by the KYC flow."},
    {"name": "kyc", "description": "Sumsub identity verification: widget token, status sync, review webhook."},
    {"name": "exchange", "description": "Exchange API keys: encrypted at rest, masked on read, verified against Binance."},
    {"name": "purchases", "description": "Recurring purchase schedules (DCA). Amounts in USDT; executed by the scheduler."},
    {"name": "transactions", "description": "Execution history with pagination, filters, and aggregate statistics."},
    {"name": "dashboard", "description": "Holdings, upcoming purchases, and investment analytics."},
    {"name": "webhooks", "description": "Inbound provider callbacks."},
    {"name": "test-purchase", "description": "TEMPORARY manual execution endpoints — removed before launch."},
]

# Create FastAPI app
app = FastAPI(
    title="AutoBuy Crypto API",
    version="0.2.0",
    description=(
        "Automated cryptocurrency purchasing platform: schedule recurring "
        "USDT buys (daily/weekly/biweekly/monthly), executed on Binance with "
        "KYC gating, encrypted key storage, and full transaction history.\n\n"
        "All protected endpoints expect `Authorization: Bearer <access token>`. "
        "Validation failures return **422**; rate limits return **429** with "
        "`Retry-After`. Full guides: `docs/API.md`, `docs/DEPLOYMENT.md`."
    ),
    contact={"name": "AutoBuy support", "email": "support@minastech.ca"},
    license_info={"name": "MIT"},
    servers=[
        {"url": "http://localhost:8000", "description": "Development"},
        {"url": "https://api.autobuy.app", "description": "Production"},
    ],
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

# Rate limiting (slowapi): default 60/min per key, stricter limits per route
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from api.services.rate_limit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# Tracing + body-size guard + security headers, one middleware pass
app.middleware("http")(platform_middleware)

# Unhandled exceptions: JSON 500 with request id, no stack trace to the client
app.add_exception_handler(Exception, unhandled_exception_handler)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health check endpoint
@app.get("/health")
@limiter.exempt
async def health_check():
    """Health check endpoint for monitoring."""
    from api.services.recurring_scheduler import scheduler
    return {
        "status": "healthy",
        "environment": settings.api_env,
        "version": "1.0.0",
        "scheduler": {
            "running": scheduler.running,
            "dry_run": scheduler.dry_run,
            "last_tick_at": scheduler.last_tick_at.isoformat() if scheduler.last_tick_at else None,
        },
    }

# Prometheus metrics
@app.get("/metrics", include_in_schema=False)
@limiter.exempt
async def metrics():
    from fastapi.responses import Response
    from api.services.metrics import render_latest
    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)


# Root endpoint
@app.get("/")
async def root():
    """Welcome endpoint."""
    return {
        "message": "AutoBuy Crypto API",
        "docs": "/docs",
        "openapi_schema": "/openapi.json"
    }

from api.routes import auth, dashboard, exchange, purchases, transactions, user

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(user.router, prefix="/api/user", tags=["user"])
app.include_router(exchange.router, prefix="/api/exchange", tags=["exchange"])
app.include_router(purchases.router, prefix="/api/purchases", tags=["purchases"])
app.include_router(transactions.router, prefix="/api/transactions", tags=["transactions"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
from api.routes import analytics
app.include_router(analytics.router, prefix="/api/analytics", tags=["dashboard"])
from api.routes import kyc
app.include_router(kyc.router, prefix="/api/kyc", tags=["kyc"])
from api.routes import webhooks
app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])

# TEMPORARY (remove in Phase 3): manual purchase-execution routes for testing
from api.routes import test_purchase
app.include_router(test_purchase.router, prefix="/test-purchase", tags=["test-purchase"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_env == "development",
        log_level=settings.log_level.lower()
    )
