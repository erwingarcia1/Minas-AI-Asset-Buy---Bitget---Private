from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from api.config import settings
from api.database import init_db, close_db, get_db

# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
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

# Create FastAPI app
app = FastAPI(
    title="AutoBuy Crypto API",
    description="Automated crypto purchasing platform",
    version="1.0.0",
    lifespan=lifespan
)

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
