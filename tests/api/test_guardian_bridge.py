"""The API's door into the Guardian engine runs on its own small thread pools.

The engine database keeps one psycopg2 connection per thread it is used from,
so the bridge must not borrow asyncio's default pool (up to 32 threads per
gunicorn worker on a server with max_connections 100): two request threads
and one seeding thread, three connections per worker. `run_db` hands the
engine its database on the bridge thread, never on the event loop, and
retries once when the connection was lost under the call; the operator pager
records the engine's pages in guardian_operator_pages with no channel
configured.
"""
import asyncio
import threading
import uuid

import psycopg2
import pytest
from sqlalchemy import text

from api.services import guardian_bridge


@pytest.fixture(autouse=True)
def close_engine_connections():
    """Each test runs on a fresh event loop; the bridge's connections (and the
    operator alerter built on them) are closed after every test."""
    yield
    guardian_bridge.close()


def test_the_bridge_pools_are_two_request_threads_and_one_seeding_thread():
    assert guardian_bridge.BRIDGE_THREADS == 2 and guardian_bridge.SEED_THREADS == 1
    assert guardian_bridge._executor._max_workers == 2
    assert guardian_bridge._seed_executor._max_workers == 1


async def test_run_executes_on_a_bridge_thread_and_returns_the_value():
    seen = {}

    def work(a, *, b):
        seen["thread"] = threading.current_thread().name
        return a + b

    assert await guardian_bridge.run(work, 2, b=3) == 5
    assert seen["thread"].startswith("guardian-bridge")


async def test_concurrent_runs_share_the_pool_without_deadlock():
    results = await asyncio.gather(*(guardian_bridge.run(lambda i=i: i * i) for i in range(6)))
    assert sorted(results) == [0, 1, 4, 9, 16, 25]


# --- run_db: the database is the bridge thread's, and a lost connection is retried once ---


class _Lost(psycopg2.OperationalError):
    """What psycopg2 raises when the server dropped the connection: no SQLSTATE."""


class _ServerAnswered(psycopg2.OperationalError):
    """An OperationalError the server answered on a live connection (a serialization failure)."""
    pgcode = "40001"


async def test_run_db_looks_the_database_up_inside_the_bridge_thread(monkeypatch):
    seen = {}
    sentinel = object()

    def fake_db():
        seen["db_thread"] = threading.current_thread().name
        return sentinel

    monkeypatch.setattr(guardian_bridge, "db", fake_db)

    def work(gdb, a, *, b):
        seen["fn_thread"] = threading.current_thread().name
        seen["gdb"] = gdb
        return a + b

    assert await guardian_bridge.run_db_read(work, 2, b=3) == 5
    assert seen["gdb"] is sentinel
    assert seen["db_thread"].startswith("guardian-bridge") and seen["fn_thread"] == seen["db_thread"]
    assert seen["db_thread"] != threading.current_thread().name, "never on the event-loop thread"


async def test_run_db_background_uses_the_single_seeding_thread(monkeypatch):
    monkeypatch.setattr(guardian_bridge, "db", lambda: "gdb")
    names = await asyncio.gather(*(guardian_bridge.run_db_background(lambda gdb: threading.current_thread().name)
                                   for _ in range(3)))
    assert all(name.startswith("guardian-seed") for name in names) and len(set(names)) == 1


async def test_run_db_retries_once_when_the_connection_was_lost(monkeypatch, caplog):
    monkeypatch.setattr(guardian_bridge, "db", lambda: "gdb")
    calls = []

    def flaky(gdb):
        calls.append(gdb)
        if len(calls) == 1:
            raise _Lost("server closed the connection unexpectedly")
        return "ok"

    with caplog.at_level("WARNING"):
        assert await guardian_bridge.run_db_read(flaky) == "ok"
    assert calls == ["gdb", "gdb"]
    assert "retrying once on a fresh connection" in caplog.text


async def test_run_db_gives_up_after_the_one_retry(monkeypatch):
    monkeypatch.setattr(guardian_bridge, "db", lambda: "gdb")
    calls = []

    def dead(gdb):
        calls.append(1)
        raise _Lost("could not connect to server")

    with pytest.raises(psycopg2.OperationalError):
        await guardian_bridge.run_db_read(dead)
    assert len(calls) == 2


async def test_run_db_does_not_retry_an_error_the_server_answered(monkeypatch):
    monkeypatch.setattr(guardian_bridge, "db", lambda: "gdb")
    calls = []

    def refused(gdb):
        calls.append(1)
        raise _ServerAnswered("could not serialize access due to concurrent update")

    with pytest.raises(psycopg2.OperationalError):
        await guardian_bridge.run_db_read(refused)
    assert calls == [1], "a live connection answered: repeating the statement is not what was asked"


async def test_run_db_does_not_retry_other_exceptions(monkeypatch):
    monkeypatch.setattr(guardian_bridge, "db", lambda: "gdb")
    calls = []

    def boom(gdb):
        calls.append(1)
        raise ValueError("no")

    with pytest.raises(ValueError):
        await guardian_bridge.run_db_read(boom)
    assert calls == [1]


# --- the operator pager ---


async def test_the_operator_pager_records_a_page_with_no_channel_configured(db, caplog):
    """The engine pages Minas through whatever `operator_alerter` it is handed;
    the bridge's one writes the guardian_operator_pages row whether or not a
    channel is configured, and is built on the bridge thread that pages."""
    scope = f"bridge-test:{uuid.uuid4().hex}"
    with caplog.at_level("ERROR"):
        page_id = await guardian_bridge.run_db_read(
            lambda gdb: guardian_bridge.operator_pager().page(
                rule="bridge_test_page", severity="LOW", message="a test page from the bridge",
                evidence={"scope": scope}, dedup_scope=scope))
    try:
        assert isinstance(page_id, int)
        row = (await db.execute(text("SELECT rule, severity, account_id, delivered_to, message, acknowledged_at "
                                     "FROM guardian_operator_pages WHERE id = :id"), {"id": page_id})).mappings().one()
        await db.commit()
        assert row["rule"] == "bridge_test_page" and row["severity"] == "LOW" and row["account_id"] is None
        assert row["delivered_to"] == 0 and row["acknowledged_at"] is None
        assert "had no deliverable channel" in caplog.text, "recorded, and the missing channel is said out loud"
    finally:
        await db.execute(text("DELETE FROM guardian_operator_pages WHERE id = :id"), {"id": page_id})
        await db.commit()
    left = (await db.execute(text("SELECT count(*) FROM guardian_operator_pages WHERE id = :id"), {"id": page_id})).scalar_one()
    await db.commit()
    assert left == 0


async def test_run_db_does_not_retry_a_write(monkeypatch):
    """A write such as onboard_account is not idempotent: a connection dropped after its INSERT
    committed would make a second attempt look like a duplicate, so only reads retry."""
    monkeypatch.setattr(guardian_bridge, "db", lambda: "gdb")
    calls = []

    def flaky(gdb):
        calls.append(gdb)
        raise _Lost("server closed the connection unexpectedly")

    with pytest.raises(_Lost):
        await guardian_bridge.run_db(flaky)
    assert calls == ["gdb"], "a write is attempted once"
