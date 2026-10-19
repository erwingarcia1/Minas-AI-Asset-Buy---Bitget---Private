"""The API's one door into the Guardian engine (v0.9.2).

The engine (`guardian/`) is synchronous: psycopg2 through
`guardian.db.Database` and the platform cipher through `guardian.crypto`. The
API is asyncio. This module holds the one shared `Database` (opened on first
use, never at import, so an API process that never serves a Guardian route
never opens an engine connection), the cipher, and `run`, which calls any
engine function in a worker thread and awaits it.

Nothing else lives here. Every rule about a monitored account is the
engine's; the routes only map its answers to HTTP.
"""
from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from api.config import settings
from guardian.crypto import PlatformCipher
from guardian.db import Database, platform_dsn

__all__ = ["BRIDGE_THREADS", "cipher", "close", "db", "run"]

T = TypeVar("T")

_lock = threading.Lock()
_db: Optional[Database] = None
_cipher = PlatformCipher()


def db() -> Database:
    """The process-wide engine database, opened on the first call. It
    connects per thread, so each worker thread `run` lands on gets its own
    connection, the way the engine's own worker pool does."""
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                _db = Database(platform_dsn(settings.database_url))
    return _db


def cipher() -> PlatformCipher:
    """The platform cipher the engine stores monitoring keys with."""
    return _cipher


# Two threads, not asyncio's default pool: the engine database opens one psycopg2
# connection per thread it is used from and keeps it, so the default pool (up to 32
# threads per gunicorn worker) could add 32 connections per worker to a server whose
# max_connections is 100. Two per worker (eight on the box) is the whole Guardian
# API budget; requests queue behind them, which is fine for a settings page.
BRIDGE_THREADS = 2
_executor = ThreadPoolExecutor(max_workers=BRIDGE_THREADS, thread_name_prefix="guardian-bridge")


async def run(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a synchronous engine function on the bridge's own small thread pool and await it."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))


def close() -> None:
    """Close every engine connection this process opened and forget the
    database; the next `db()` opens a fresh one. For shutdown and for tests,
    whose event loop (and thread pool) changes per test."""
    global _db
    with _lock:
        database, _db = _db, None
    if database is not None:
        database.close()
