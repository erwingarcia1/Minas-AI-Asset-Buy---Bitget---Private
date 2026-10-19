"""The worker's plumbing: heartbeat, pause switch, digest-once-a-day, and the
engine hooks that call them. The transports are blank (conftest), so nothing
here can reach a provider."""
import json
import threading
from datetime import datetime, timezone

from api.config import settings
from guardian.digest_job import LAST_SENT_KEY, DailyDigest
from guardian.engine import Engine
from guardian.heartbeat import HEARTBEAT_KEY, PAUSE_KEY, Heartbeat, heartbeat_age_seconds, read_heartbeat
from tests.guardian.conftest import make_account


def _redis():
    import redis
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def _clear():
    _redis().delete(HEARTBEAT_KEY, PAUSE_KEY, LAST_SENT_KEY)


def test_heartbeat_is_written_with_a_ttl_and_the_file_is_touched(tmp_path):
    _clear()
    hb = Heartbeat(settings.redis_url, enabled=False, commit="abc1234", file_path=str(tmp_path / "beat"))
    assert hb.beat({"idle": True}) is True
    raw = _redis().get(HEARTBEAT_KEY)
    payload = json.loads(raw)
    assert payload["enabled"] is False and payload["idle"] is True and payload["commit"] == "abc1234"
    assert 0 < _redis().ttl(HEARTBEAT_KEY) <= 180
    assert (tmp_path / "beat").exists()
    assert heartbeat_age_seconds(read_heartbeat(settings.redis_url)) < 5


def test_pause_switch_reads_the_key_and_fails_closed_when_redis_is_unreachable(tmp_path):
    _clear()
    hb = Heartbeat(settings.redis_url, enabled=True, file_path=str(tmp_path / "beat"))
    assert hb.paused() is False
    _redis().set(PAUSE_KEY, json.dumps({"since": "now", "by": "admin"}))
    assert hb.paused() is True
    broken = Heartbeat("redis://127.0.0.1:1/1", enabled=True, file_path=str(tmp_path / "beat2"))
    assert broken.paused() is True, "unreadable = paused"
    assert broken.beat({}) is False, "a heartbeat that cannot be written is reported, not raised"
    _clear()


class _Hooks:
    def __init__(self, paused):
        self._paused = paused
        self.beats = []
        self.ticks = []

    def paused(self):
        return self._paused

    def beat(self, snapshot):
        self.beats.append(snapshot)

    def after_tick(self, paused):
        self.ticks.append(paused)


def test_a_paused_engine_queues_no_polls_but_still_beats(db, cipher, rules):
    make_account(db)
    hooks = _Hooks(paused=True)
    engine = Engine(db, cipher, rules, {}, hooks=hooks)
    engine._scheduler_tick()
    assert engine._queue.qsize() == 0
    assert hooks.ticks == [True]
    assert len(hooks.beats) == 1 and hooks.beats[0]["paused"] is True


def test_a_running_engine_queues_polls_and_reports_the_count(db, cipher, rules):
    make_account(db, "a")
    make_account(db, "b")
    hooks = _Hooks(paused=False)
    engine = Engine(db, cipher, rules, {}, hooks=hooks)
    engine._scheduler_tick()
    assert engine._queue.qsize() == 4, "a fast and a slow task per active account"
    assert hooks.beats[0]["active_accounts"] == 2 and hooks.beats[0]["paused"] is False
    assert hooks.beats[0]["rules_valid"] is True
    assert hooks.ticks == [False]


def test_engine_join_returns_once_workers_stop(db, cipher, rules):
    engine = Engine(db, cipher, rules, {})
    runner = threading.Thread(target=engine.run, kwargs={"pool_size": 2}, daemon=True)
    runner.start()
    import time
    time.sleep(0.2)   # let run() start its threads before asking it to stop
    engine.stop()
    engine.join(5.0)
    runner.join(5.0)
    assert not runner.is_alive()


class _Recording:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def test_daily_digest_sends_once_after_the_hour_and_records_the_date(db):
    _clear()
    account_id = make_account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, "
                    "confidence, sent_at) VALUES (%s, 'baseline_drift', 'LOW', 'k', 'drift', '{}', 'low', %s)",
                    (account_id, datetime.now(timezone.utc).isoformat()))
    channel = _Recording()
    digest = DailyDigest(db, lambda: [channel], hour_utc=8, redis_url=settings.redis_url)
    before = datetime(2026, 10, 13, 7, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 13, 8, 1, tzinfo=timezone.utc)
    assert digest.run_if_due(before) is False and channel.sent == []
    assert digest.run_if_due(after) is True and len(channel.sent) == 1
    assert "Guardian daily digest" in channel.sent[0].message
    assert _redis().get(LAST_SENT_KEY) == "2026-10-13"
    assert digest.run_if_due(after) is False and len(channel.sent) == 1, "once a day"
    _clear()


def test_daily_digest_with_nothing_to_report_still_marks_the_day(db):
    _clear()
    channel = _Recording()
    digest = DailyDigest(db, lambda: [channel], hour_utc=0, redis_url=settings.redis_url)
    assert digest.run_if_due(datetime(2026, 10, 13, 9, tzinfo=timezone.utc)) is False
    assert channel.sent == [] and _redis().get(LAST_SENT_KEY) == "2026-10-13"
    _clear()


def test_idle_worker_only_heartbeats(tmp_path, monkeypatch, db):
    # `db` truncates the guardian tables first: the empty-database assertion below is this
    # test's own, not something the previous test's teardown happened to leave behind.
    from guardian import worker
    _clear()
    monkeypatch.setattr(worker, "IDLE_BEAT_SECONDS", 0.05)
    hb = Heartbeat(settings.redis_url, enabled=False, file_path=str(tmp_path / "beat"))
    stop = threading.Event()
    t = threading.Thread(target=worker._idle, args=(hb, stop), daemon=True)
    t.start()
    stop.set()
    t.join(2.0)
    payload = json.loads(_redis().get(HEARTBEAT_KEY))
    assert payload["enabled"] is False and payload.get("stopping") is True
    assert _guardian_rows() == 0, "flag off writes nothing to the database"
    _clear()


def _guardian_rows() -> int:
    """Row count across every guardian table, read with a fresh connection."""
    import os

    from guardian.db import Database, platform_dsn
    from tests.guardian.conftest import GUARDIAN_TABLES
    database = Database(platform_dsn(os.environ["DATABASE_URL"]))
    try:
        total = 0
        with database.cursor() as cur:
            for table in GUARDIAN_TABLES:
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                total += cur.fetchone()["n"]
        return total
    finally:
        database.close()


class _FakeEngine:
    """Stands in for Engine in the supervisor tests: run() blocks until stop() and then takes a
    moment to return, like a half-done scheduler pass; join() records the budget it was given."""

    def __init__(self):
        self._stop = threading.Event()
        self.joined_with = None

    def run(self):
        self._stop.wait(10.0)
        import time
        time.sleep(0.2)

    def stop(self):
        self._stop.set()

    def join(self, timeout):
        self.joined_with = timeout


class _FakeDb:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_supervisor_exits_non_zero_when_the_engine_dies_on_its_own(tmp_path):
    """The engine thread ending without a signal is a crash, not a stop. compose restarts the
    container either way (restart: always); the exit code and the log line say which it was,
    and the shutdown sequence still runs: stop, join, the "stopping" heartbeat, db.close()."""
    from guardian import worker
    _clear()
    engine = _FakeEngine()
    runner = threading.Thread(target=lambda: None, name="engine", daemon=True)   # returns at once
    hb = Heartbeat(settings.redis_url, enabled=True, file_path=str(tmp_path / "beat"))
    db = _FakeDb()
    assert worker._supervise(engine, runner, threading.Event(), hb, db, join_timeout=2.0) == 1
    assert engine._stop.is_set() and engine.joined_with == 2.0 and db.closed
    assert json.loads(_redis().get(HEARTBEAT_KEY)).get("stopping") is True
    _clear()


def test_supervisor_joins_the_engine_thread_before_the_stopping_heartbeat(tmp_path):
    """SIGTERM: the engine thread is joined (bounded) BEFORE the final heartbeat, so "stopping"
    is the last thing the process writes and the thread cannot outlive it mid-pass. The fake
    engine lingers 200 ms after stop(), so without the join it would still be alive here."""
    from guardian import worker
    _clear()
    engine = _FakeEngine()
    runner = threading.Thread(target=engine.run, name="engine", daemon=True)
    stop = threading.Event()
    seen = {}

    class _Hb(Heartbeat):
        def stopping(self):
            seen["runner_alive_at_stopping"] = runner.is_alive()
            super().stopping()

    hb = _Hb(settings.redis_url, enabled=True, file_path=str(tmp_path / "beat"))
    threading.Timer(0.2, stop.set).start()
    assert worker._supervise(engine, runner, stop, hb, _FakeDb(), join_timeout=5.0) == 0
    assert seen == {"runner_alive_at_stopping": False}
    assert not runner.is_alive()
    assert json.loads(_redis().get(HEARTBEAT_KEY)).get("stopping") is True
    _clear()
