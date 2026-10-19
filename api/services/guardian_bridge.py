"""The API's one door into the Guardian engine (v0.9.2).

The engine (`guardian/`) is synchronous: psycopg2 through
`guardian.db.Database` and the platform cipher through `guardian.crypto`. The
API is asyncio. This module holds the one shared `Database` (opened on first
use, never at import, so an API process that never serves a Guardian route
never opens an engine connection), the cipher, the operator pager the engine
records its pages through, and the calls that run an engine function on a
bridge thread and await it:

* `run_db(fn, *args, **kwargs)` calls `fn(db(), *args, **kwargs)` on one of
  the two request threads, with `db()` evaluated INSIDE that thread. A
  `Database` connects in its constructor and keeps one connection per thread,
  so a `db()` call from a coroutine would connect on the event-loop thread:
  blocking the loop, and opening a connection no bridge thread ever uses.
  The routes never call `db()`; it stays public for tests.
* `run_db_background` is the same on the single seeding thread, so a slow
  exchange during a baseline seed never holds a request thread.
* `run(fn, *args, **kwargs)` runs any function on a request thread, for a
  caller that brings its own arguments.

A call whose connection the server dropped (a Postgres restart during a
deploy) is retried once on a fresh connection: the engine's `Database`
replaces a dead connection as the error passes through it, so the second
attempt starts clean. An error the server answered on a live connection is
not retried.

Nothing else lives here. Every rule about a monitored account is the
engine's; the routes only map its answers to HTTP.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

import psycopg2

import guardian
from api.config import settings
from guardian.crypto import PlatformCipher
from guardian.db import Database, platform_dsn

__all__ = ["BRIDGE_THREADS", "SEED_THREADS", "cipher", "close", "db", "operator_pager", "run", "run_db",
           "run_db_background"]

logger = logging.getLogger("autobuy.guardian_bridge")

T = TypeVar("T")

_lock = threading.Lock()
_db: Optional[Database] = None
_alerter_lock = threading.Lock()
_alerter = None
_cipher = PlatformCipher()


def db() -> Database:
    """The process-wide engine database, opened on the first call. It
    connects per thread, so each bridge thread gets its own connection, the
    way the engine's own worker pool does. Called on a bridge thread (`run_db`
    does that); the routes never call it from the event loop."""
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                _db = Database(platform_dsn(settings.database_url))
    return _db


def cipher() -> PlatformCipher:
    """The platform cipher the engine stores monitoring keys with."""
    return _cipher


# Three threads, not asyncio's default pool: the engine database opens one psycopg2
# connection per thread it is used from and keeps it, so the default pool (up to 32
# threads per gunicorn worker) could add 32 connections per worker to a server whose
# max_connections is 100. Two request threads plus one for the background baseline
# seed, three per worker (twelve on the box), is the whole Guardian API budget:
# requests queue behind the two, which is fine for a settings page, and a slow
# exchange during a seed holds only the third. docs/DEPLOYMENT.md carries the
# connection arithmetic.
BRIDGE_THREADS = 2
SEED_THREADS = 1
_executor = ThreadPoolExecutor(max_workers=BRIDGE_THREADS, thread_name_prefix="guardian-bridge")
_seed_executor = ThreadPoolExecutor(max_workers=SEED_THREADS, thread_name_prefix="guardian-seed")


async def run(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a synchronous function on a bridge request thread and await it."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))


def _connection_lost(exc: psycopg2.OperationalError) -> bool:
    """Did the connection die under the call (the server restarted, the
    network went) rather than the server answering an error? A dropped
    connection carries no SQLSTATE; class 08 is a connection exception and
    57P an operator shutting the server down. Anything else (a lock timeout,
    a cancelled query) is an answer on a live connection and is not retried:
    repeating the statement is not what the caller asked for."""
    code = getattr(exc, "pgcode", None)
    return code is None or code.startswith(("08", "57P"))


def _with_db(fn: Callable[..., T], args: tuple, kwargs: dict) -> T:
    """`fn(db(), *args, **kwargs)` on this thread, once more if the
    connection was lost. The engine's `Database.cursor` has replaced the dead
    connection by the time the error gets here, so the retry starts clean."""
    try:
        return fn(db(), *args, **kwargs)
    except psycopg2.OperationalError as exc:
        if not _connection_lost(exc):
            raise
        logger.warning("guardian bridge: the engine connection was lost during %s (%s); retrying once on a fresh connection",
                       getattr(fn, "__name__", "an engine call"), type(exc).__name__)
        return fn(db(), *args, **kwargs)


async def run_db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call `fn(db(), *args, **kwargs)` on a bridge request thread and await
    it. The database is looked up inside that thread, never on the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, functools.partial(_with_db, fn, args, kwargs))


async def run_db_background(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """`run_db` on the single seeding thread: for the baseline seed, which
    makes several exchange calls and must not hold a request thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_seed_executor, functools.partial(_with_db, fn, args, kwargs))


class _OperatorPager:
    """What the routes hand the engine as `operator_alerter`. `page(...)`
    records the page in `guardian_operator_pages` and sends it where the
    platform has an operator channel configured (the `operator_alerting`
    routing the worker uses); with none configured the row is still written,
    so a refusal that tells the customer Minas has been told is true. The
    engine's `OperatorAlerter` is built on first use, on the bridge thread
    that pages, so the event loop never touches the Database. It has no
    customer route at all: a page never reaches the account it is about."""

    def page(self, *args: Any, **kwargs: Any) -> Optional[int]:
        return _operator_alerter().page(*args, **kwargs)


_pager = _OperatorPager()


def operator_pager() -> _OperatorPager:
    """The engine-facing operator pager; safe to hand over from a coroutine,
    since it opens nothing until the engine pages through it."""
    return _pager


def _rules_path() -> str:
    """The engine's rules file, for the pager's routing and cooldowns:
    ``GUARDIAN_RULES_PATH`` when it names a file, otherwise the file shipped
    with the package, which is what an empty setting means (`.env.example`,
    docs/DEPLOYMENT.md). An empty string reaches `settings` as '' and
    `Path('')` is the current directory, so this never hands `RulesConfig`
    anything but a file."""
    configured = (settings.guardian_rules_path or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    return str(pathlib.Path(guardian.__file__).resolve().parent / "rules.yaml")


def _operator_alerter():
    global _alerter
    if _alerter is None:
        database = db()   # before the lock: db() takes its own
        with _alerter_lock:
            if _alerter is None:
                from guardian.alerts.operator import OperatorAlerter
                from guardian.channels import build_operator_channels
                from guardian.config import RulesConfig

                rules = RulesConfig(_rules_path())
                _alerter = OperatorAlerter(database, build_operator_channels(rules, db=database), rules)
    return _alerter


def close() -> None:
    """Close every engine connection this process opened and forget the
    database and the operator alerter built on it; the next `db()` opens a
    fresh one. For shutdown and for tests, whose event loop (and thread
    pool) changes per test."""
    global _db, _alerter
    with _alerter_lock:
        _alerter = None
    with _lock:
        database, _db = _db, None
    if database is not None:
        database.close()
