from fastapi import FastAPI, Depends, HTTPException, Request, status
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
    from api.integrations.resend import check_startup_config
    check_startup_config()
    await init_db()
    print("✓ Database initialized")
    if settings.scheduler_enabled:
        # Auto-Buy purchases: APScheduler, every 5 minutes (+ weekly digest cron)
        import logging as _logging
        import os as _os
        from api.tasks.purchase_scheduler import build_scheduler
        app.state.autobuy_scheduler = build_scheduler()
        app.state.autobuy_scheduler.start()
        job = app.state.autobuy_scheduler.get_job("execute_purchases")
        _logging.getLogger("autobuy.tasks.purchases").info(
            "Auto-Buy purchase scheduler started (pid=%s, every 5 minutes, first run %s, "
            "misfire_grace_time=%ss, dry_run=%s)", _os.getpid(), job.next_run_time, job.misfire_grace_time,
            settings.autobuy_dry_run)
        print(f"✓ Auto-Buy purchase scheduler started (every 5 minutes, dry_run={settings.autobuy_dry_run})")
    yield
    # Shutdown
    if settings.scheduler_enabled:
        autobuy = getattr(app.state, "autobuy_scheduler", None)
        if autobuy is not None:
            autobuy.shutdown(wait=False)
            app.state.autobuy_scheduler = None
    await close_db()
    print("✓ Database closed")

OPENAPI_TAGS = [
    {"name": "auth", "description": "Registration, login, JWT lifecycle (refresh, logout, password change)."},
    {"name": "user", "description": "Profile management. `kyc_status` is read-only here — it is owned by the KYC flow."},
    {"name": "kyc", "description": "Sumsub identity verification: widget token, status sync, review webhook."},
    {"name": "exchange", "description": "Exchange API keys: encrypted at rest, masked on read, verified against Binance."},
    {"name": "transactions", "description": "Execution history with pagination, filters, and aggregate statistics."},
    {"name": "dashboard", "description": "Holdings, upcoming purchases, and investment analytics."},
    {"name": "webhooks", "description": "Inbound provider callbacks."},
    {"name": "auto-buy", "description": "Auto-Buy purchases delivered to validated wallets (multi-network) and saved wallets."},
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
    contact={"name": f"{settings.app_name} support", **({"email": settings.support_email} if settings.support_email else {})},
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
def build_commit() -> str:
    """Commit stamped into the image by Dockerfile.api (GIT_COMMIT build arg)."""
    import pathlib
    for candidate in (pathlib.Path(__file__).resolve().parent.parent / "GIT_COMMIT", pathlib.Path("/app/GIT_COMMIT")):
        try:
            value = candidate.read_text().strip()
            if value:
                return value
        except OSError:
            continue
    return "unknown"


@app.get("/health")
@limiter.exempt
async def health_check(request: Request):
    """Health check endpoint for monitoring.

    ``schedulers`` reports the admin pause switch and the Auto-Buy
    execute_purchases job from the Redis heartbeat, so the values are the same
    whichever worker answers: running / paused, last run time, last result
    (``ok`` | ``failed`` | ``error`` | ``skipped_paused``), last counts and the
    consecutive-failure count.
    """
    from api.services import scheduler_state
    from api.tasks.purchase_scheduler import INTERVAL_MINUTES, WITHDRAWAL_POLL_INTERVAL_MINUTES
    try:
        pause = await scheduler_state.pause_info()
    except scheduler_state.PauseStateUnavailable:
        pause = {"paused": None, "since": None, "by": None}
    autobuy = await scheduler_state.status("autobuy")
    withdrawals = await scheduler_state.status("withdrawals")
    autobuy_sched = getattr(request.app.state, "autobuy_scheduler", None)
    return {
        "status": "healthy",
        "environment": settings.api_env,
        "version": "1.0.0",
        "commit": build_commit(),   # the git commit this image was built from ("unknown" outside a stamped image)
        "dry_run": settings.autobuy_dry_run,
        "schedulers": {
            "paused": pause["paused"],           # None = Redis unreadable (the job fails closed)
            "paused_since": pause["since"],
            "paused_by": pause["by"],
            "autobuy": {
                "running": bool(autobuy_sched is not None and autobuy_sched.running),
                "paused": pause["paused"],
                "dry_run": settings.autobuy_dry_run,
                "interval_minutes": INTERVAL_MINUTES,
                "last_run_at": autobuy["last_run_at"],
                "last_result": autobuy["last_result"],
                "last_detail": autobuy["last_detail"],
                "last_counts": autobuy["last_counts"],
                "consecutive_failures": autobuy["consecutive_failures"],
            },
            # confirms sent withdrawals with the exchange (tx hash / final status)
            "withdrawals": {
                "running": bool(autobuy_sched is not None and autobuy_sched.running),
                "paused": pause["paused"],
                "interval_minutes": WITHDRAWAL_POLL_INTERVAL_MINUTES,
                "last_run_at": withdrawals["last_run_at"],
                "last_result": withdrawals["last_result"],
                "last_counts": withdrawals["last_counts"],
            },
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

from api.routes import auth, dashboard, exchange, transactions, user, public

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(public.router, prefix="/api/public", tags=["public"])
app.include_router(user.router, prefix="/api/user", tags=["user"])
app.include_router(exchange.router, prefix="/api/exchange", tags=["exchange"])
app.include_router(transactions.router, prefix="/api/transactions", tags=["transactions"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
from api.routes import integrations
app.include_router(integrations.router, prefix="/api/integrations", tags=["webhooks"])
from api.routes import reports
app.include_router(reports.router, prefix="/api/reports", tags=["dashboard"])
from api.routes import notifications
app.include_router(notifications.router, prefix="/api/notifications", tags=["user"])
from api.routes import admin
app.include_router(admin.router, prefix="/api/admin", tags=["user"])
from api.routes import analytics
app.include_router(analytics.router, prefix="/api/analytics", tags=["dashboard"])
from api.routes import kyc
app.include_router(kyc.router, prefix="/api/kyc", tags=["kyc"])
from api.routes import webhooks
app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
from api.routes import email
app.include_router(email.router, prefix="/api/email", tags=["user"])
from api.routes import autobuy_purchases, wallets
app.include_router(autobuy_purchases.router, prefix="/api/v2/purchases", tags=["auto-buy"])
app.include_router(wallets.router, prefix="/api/wallets", tags=["auto-buy"])
from api.routes import market
app.include_router(market.router, prefix="/api/v2/market", tags=["auto-buy"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_env == "development",
        log_level=settings.log_level.lower()
    )
