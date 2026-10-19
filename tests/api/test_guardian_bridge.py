"""The API's door into the Guardian engine runs on its own small thread pool.

The engine database keeps one psycopg2 connection per thread it is used from,
so the bridge must not borrow asyncio's default pool (up to 32 threads per
gunicorn worker on a server with max_connections 100)."""
import asyncio
import threading

from api.services import guardian_bridge


def test_the_bridge_pool_is_two_threads():
    assert guardian_bridge.BRIDGE_THREADS == 2
    assert guardian_bridge._executor._max_workers == 2


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
