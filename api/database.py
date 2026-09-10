from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import event
import os
from dotenv import load_dotenv

if os.getenv("AUTOBUY_SKIP_DOTENV") != "1":   # tests set this: they own every variable they need
    load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://autobuy:autobuy_password@localhost:5432/autobuy_db"
)

# Real connection pool by default (NullPool reconnects on every session,
# which dominates latency under load). Tests set AUTOBUY_DB_POOL=null because
# pooled asyncpg connections are bound to the event loop that created them
# and pytest-asyncio gives every test a fresh loop.
if os.getenv("AUTOBUY_DB_POOL", "").lower() == "null":
    from sqlalchemy.pool import NullPool
    _pool_kwargs = {"poolclass": NullPool}
else:
    _pool_kwargs = {"pool_size": 20, "max_overflow": 10,
                    "pool_recycle": 3600, "pool_pre_ping": True}

engine = create_async_engine(DATABASE_URL, echo=False, **_pool_kwargs)

# Session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Slow-query surfacing: anything over 1s is worth a warning line.
import logging
import time as _time

_slow_log = logging.getLogger("autobuy.db")


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _q_start(conn, cursor, statement, parameters, context, executemany):
    context._query_start = _time.perf_counter()


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _q_end(conn, cursor, statement, parameters, context, executemany):
    elapsed = _time.perf_counter() - getattr(context, "_query_start", _time.perf_counter())
    if elapsed > 1.0:
        _slow_log.warning("slow_query", extra={
            "duration_s": round(elapsed, 2), "statement": statement[:200]})


# Base for models
Base = declarative_base()


async def get_db():
    """Dependency for FastAPI to get database session"""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    """Create tables in dev/test. Production schema is managed exclusively by
    Alembic (`alembic upgrade head`); create_all would bypass versioning."""
    if os.getenv("API_ENV", "development") == "production":
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """Close database connection"""
    await engine.dispose()
