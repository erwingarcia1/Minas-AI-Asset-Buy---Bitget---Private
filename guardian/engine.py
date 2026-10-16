from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from guardian.circuit import BreakerRegistry
from guardian.config import RulesConfig
from guardian.crypto import CredentialCipher
from guardian.db import Database
from guardian.poller import AccountPoller
from guardian.ratelimit import RateLimiters
from guardian.retention import prune_all, table_counts
from guardian.rules_schema import default_for

logger = logging.getLogger("guardian.engine")

# The heartbeat's own cadence, apart from the poll interval. Redis keeps a beat
# for HEARTBEAT_TTL_S (180 s, guardian/heartbeat.py) and the deploy check wants
# one younger than 120 s, so the pulse cannot simply follow
# `polling.fast_loop_seconds`: a legitimate edit raising that past the TTL made
# a working worker read as a dead one. The scheduler sleeps in slices no longer
# than this and beats between them (`_sleep_with_heartbeat`).
HEARTBEAT_MAX_INTERVAL_S = 60.0


@dataclass(order=True)
class Task:
    """A unit of per-account work. Idempotent by construction: fast/slow
    loops diff against stored state, so re-running one is harmless."""

    due_at: float
    account_id: int = field(compare=False)
    kind: str = field(compare=False)  # 'fast' | 'slow'


class Engine:
    """Scheduler -> queue -> worker pool. In-process threads + queue.Queue
    for Phase 2; the Task shape and idempotency contract are designed so a
    Redis-backed distributed queue is a drop-in replacement when one process
    stops being enough (multi-host scale-out is the documented path in
    docs/deployment.md)."""

    def __init__(
        self,
        db: Database,
        cipher: CredentialCipher,
        rules: RulesConfig,
        channels: dict,
        metrics=None,
        escalator=None,
        operator_alerter=None,
        contact_router=None,
        hooks=None,
    ):
        self._db = db
        # Platform plumbing (v0.9.1): an object with `paused() -> bool`,
        # `beat(snapshot: dict)` and `after_tick(paused: bool)`. Optional so
        # the engine tests run it bare, exactly as Guardian's did.
        self._hooks = hooks
        # What the last scheduler pass found on the pause switch, so the beats
        # sent between passes (`_sleep_with_heartbeat`) say what the pass said.
        self._last_paused = False
        self._active_count = 0
        self._last_prune_at: str | None = None
        self._workers: list[threading.Thread] = []
        self._scheduler: threading.Thread | None = None
        self._cipher = cipher
        self._rules = rules
        self._metrics = metrics
        self._escalator = escalator
        # Paging scoped to Minas (G-49). Held here so the conditions that need
        # it - the untimestamped counter rising (#52), a failed drill (#37),
        # the scheduler falling behind (#42) - have somewhere to raise from.
        # Those callers belong to their own issues; this is the route.
        self._operator_alerter = operator_alerter
        # First alarm through the G-49 door (G-48). Watches the undated-record
        # counter, whose resting value is exactly zero, so the threshold is
        # derived rather than chosen: any sustained rise is a format change.
        from guardian.alarms import UntimestampedRecordAlarm

        self._untimestamped_alarm = UntimestampedRecordAlarm(
            metrics,
            operator_alerter,
            window_seconds=rules.get_seconds(
                "operator_alerting", "untimestamped_window_seconds", default=900
            ),
            consecutive_windows=rules.get_int(
                "operator_alerting", "untimestamped_consecutive_windows", default=2
            ),
        ) if metrics is not None else None
        self._queue: queue.Queue[Task] = queue.Queue()
        self._stop = threading.Event()
        # (account_id, kind) pairs that are queued or currently running.
        # The scheduler fires every fast_loop_seconds regardless of whether the
        # previous poll finished, so a poll slower than the interval - a slow
        # exchange, an open breaker, a backed-up queue - used to put two workers
        # on one account: both reading the same baseline, both judging the same
        # records, both racing to write the same poll_state (G-45).
        self._inflight: set[tuple[int, str]] = set()
        self._inflight_lock = threading.Lock()
        # Which config rejection we have already paged about, so a persistent
        # one doesn't page every scheduler cycle while a *new* one still does.
        self._paged_config_rejection: str | None = None
        # None, not 0. `time.monotonic()` is seconds since boot, so 0 as "never
        # pruned" would gate the first prune on the machine's uptime - the exact
        # defect that made CI red for ten hours.
        self._last_prune: float | None = None

        def _rate(key: str, default: str) -> float:
            return float(rules.get_decimal("rate_limits", key, default=Decimal(default)))

        self.limiters = RateLimiters(
            per_account_rps=_rate("per_account_rps", "5"),
            per_account_burst=_rate("per_account_burst", "10"),
            ip_rps=_rate("egress_ip_rps", "60"),
            ip_burst=_rate("egress_ip_burst", "100"),
        )
        self.breakers = BreakerRegistry(
            failure_threshold=rules.get_int("circuit_breaker", "failure_threshold", default=3),
            base_backoff_seconds=rules.get_seconds("circuit_breaker", "base_backoff_seconds", default=60),
            max_backoff_seconds=rules.get_seconds("circuit_breaker", "max_backoff_seconds", default=3600),
        )
        from guardian.alerts.contact import ContactRouter

        # Accepted rather than constructed here, so the SAME router serves the
        # poller and the escalator and so it carries the operator alerter it
        # needs to report a channel with no provider (#58). Constructing one
        # privately meant the escalator could not have the customer's own
        # contacts (#27) and this one could never page.
        #
        # The fallback below keeps other callers working, but a caller that
        # uses it gets a SECOND router - and `_warn_once` is per instance, so
        # "this channel has no provider" would page once per router rather
        # than once per process. Pass one in.
        self._poller = AccountPoller(
            db, cipher, rules, channels, limiter=self.limiters, metrics=metrics,
            contact_router=contact_router or ContactRouter(
                db=db, rules=rules, operator_alerter=operator_alerter),
            operator_alerter=operator_alerter,
        )
        self._last_slow: dict[int, float] = {}

    # --- scheduler -----------------------------------------------------------

    def _active_accounts(self) -> list:
        # An account whose baseline is still seeding is not polled: the
        # detectors compare against stored history, and half-seeded history
        # reads as a stranger's activity. Onboarding moves the row to
        # 'complete' or 'incomplete' when the seed finishes, and the next
        # scheduler pass picks it up.
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM guardian_accounts WHERE status = 'active' AND baseline_status <> 'seeding'"
            )
            return cur.fetchall()

    def _schedule_cycle(self) -> None:
        jitter = self._rules.get_seconds("polling", "jitter_seconds", default=10)
        slow_interval = self._rules.get_seconds("polling", "slow_loop_seconds", default=600)
        now = time.monotonic()
        active = self._active_accounts()
        self._active_count = len(active)
        if self._metrics is not None:
            self._metrics.set_active_accounts(len(active))
        for row in active:
            account_id = row["id"]
            if self._claim(account_id, "fast"):
                self._queue.put(Task(now + random.uniform(0, jitter), account_id, "fast"))
            else:
                # Not an error: the previous fast loop for this account is still
                # in flight. Skipping is the correct behaviour, but a customer
                # whose polls consistently overrun the interval is being watched
                # less often than the dashboard implies, so say so.
                logger.warning(
                    "account %d still has a fast poll in flight - skipping this cycle", account_id
                )
            # The fast and slow loops read different things and write different
            # state, so they are claimed separately: a pending slow loop must
            # never hold up the loop that watches for withdrawals.
            #
            # "Never polled" is None, not 0. `time.monotonic()` is seconds since
            # *boot* on Linux, so `now - 0 >= slow_interval` was really asking
            # "has this machine been up for ten minutes?" - a question about the
            # host that has nothing to do with this account. On a long-lived box
            # monotonic is enormous and it passed trivially; on a freshly booted
            # one the slow loop did not run at all for the first ten minutes of
            # the machine's life, which is exactly when you want a permission
            # and balance baseline established.
            last_slow = self._last_slow.get(account_id)
            due = last_slow is None or now - last_slow >= slow_interval
            if due and self._claim(account_id, "slow"):
                self._queue.put(Task(now + random.uniform(0, jitter), account_id, "slow"))
                self._last_slow[account_id] = now

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            self._sleep_with_heartbeat(self._scheduler_pass())

    def _sleep_with_heartbeat(self, seconds: float) -> None:
        """Waits `seconds` for the next pass, beating every
        HEARTBEAT_MAX_INTERVAL_S in the meantime.

        The pulse used to be the pass's one beat, so its cadence was
        `polling.fast_loop_seconds` with no ceiling, and an interval longer
        than the heartbeat TTL read as a stale worker. A wait of one slice or
        less is unchanged: no extra beat, the pass's own beat is the pulse.
        `stop()` ends the wait at once, with no beat after it."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._stop.wait(min(remaining, HEARTBEAT_MAX_INTERVAL_S)):
                return
            if deadline - time.monotonic() > 0:
                self._beat(self._last_paused)

    def _scheduler_pass(self) -> float:
        """One pass, returning how long to sleep before the next. Never raises.

        The interval read is inside the guard, not just the work. It goes
        through `RulesConfig`, which touches the filesystem and re-parses YAML -
        so it can fail, and if it did outside a handler the scheduler thread
        would die and nothing would ever be queued again. Every account would
        stop being polled while the process stayed up, the dashboard kept
        serving, and the metrics endpoint kept answering. Total silence
        presented as health, which is the failure mode this whole codebase keeps
        being audited for."""
        started = time.monotonic()
        self._scheduler_tick()
        try:
            fast_interval = self._rules.get_seconds("polling", "fast_loop_seconds")
        except Exception as exc:  # noqa: BLE001 - a bad interval must not stop scheduling
            logger.error("could not read the poll interval (%s) - using the built-in default", exc)
            fast_interval = float(default_for("polling", "fast_loop_seconds") or 45)
        return max(1.0, fast_interval - (time.monotonic() - started))

    def _scheduler_tick(self) -> None:
        """One pass of the scheduler's housekeeping: queue the due work, run
        escalation, and republish the health signals.

        Split out of the loop so a test can drive it. Asserting the pieces
        individually does not catch one being dropped from the loop, and an
        alarm that exists but is never called is the same as no alarm - the
        standard #52 set for itself.

        Everything is inside the handler, not just the queuing. These calls run
        on the scheduler thread every cycle, so an exception in any of them
        would kill the scheduler outright and stop all polling: the loudest
        possible failure expressed as complete silence."""
        paused = False
        try:
            # The admin pause switch (fail closed): paused = no polls queued
            # and no escalation this pass. Retention, config health and the
            # alarms still run - pausing stops watching, not housekeeping.
            paused = bool(self._hooks is not None and self._hooks.paused())
            if paused:
                logger.info("guardian paused: no polls queued this pass")
            else:
                self._schedule_cycle()
                if self._escalator is not None:
                    self._escalator.check(self._db)
            # Republished every cycle rather than only on change: a scrape that
            # starts after the rejection still sees the 0, and a restart that
            # re-reads a still-broken file re-asserts it.
            if self._metrics is not None:
                self._metrics.set_rules_config_valid(self._rules.is_valid)
            self._check_config_health()
            self._prune_if_due()
            if self._untimestamped_alarm is not None:
                self._untimestamped_alarm.evaluate(now=time.monotonic())
            if self._hooks is not None:
                self._hooks.after_tick(paused)
        except Exception as exc:  # noqa: BLE001 - scheduler must survive anything
            logger.error("scheduler cycle failed: %s", exc)
        self._last_paused = paused
        self._beat(paused)

    def _beat(self, paused: bool) -> None:
        """One heartbeat through the hooks. Never raises."""
        if self._hooks is None:
            return
        try:
            self._hooks.beat(self.snapshot(paused))
        except Exception as exc:  # noqa: BLE001 - the pulse must not kill the heart
            logger.error("heartbeat hook failed: %s", exc)

    def snapshot(self, paused: bool = False) -> dict:
        """What the heartbeat carries: enough for /health and the admin page
        to say whether watching is happening."""
        with self._inflight_lock:
            inflight = len(self._inflight)
        try:
            rules_valid = self._rules.is_valid
        except Exception:  # noqa: BLE001
            rules_valid = False
        return {"paused": paused, "active_accounts": self._active_count, "queue_depth": self._queue.qsize(),
                "inflight": inflight, "rules_valid": rules_valid, "last_prune_at": self._last_prune_at}

    def _prune_if_due(self) -> None:
        """Retention, on its own interval rather than the scheduler's.

        Full-table deletes on a 45-second loop would be wasted I/O against the
        very tables the fast loop queries."""
        interval = self._rules.get_seconds("retention", "prune_interval_minutes") * 60
        now = time.monotonic()
        if self._last_prune is not None and now - self._last_prune < interval:
            return
        self._last_prune = now
        prune_all(self._db, self._rules)
        from datetime import datetime, timezone
        self._last_prune_at = datetime.now(timezone.utc).isoformat()
        if self._metrics is not None:
            self._metrics.set_table_rows(table_counts(self._db))

    def _check_config_health(self) -> None:
        """Pages CRITICAL while the rules file on disk is rejected.

        This is what makes running on a rejected config defensible rather than
        merely quiet. `RulesConfig` no longer refuses to start - refusing turns
        an upgrade into an outage - so the thing that must not be missed is that
        the thresholds in force are not the ones in the file.

        Keyed on the rejection itself, so a persistent problem pages once and a
        *new* mistake pages again: an operator who fixes one error and
        introduces another has to hear about the second.
        """
        rejected_at = self._rules.rejected_at
        if rejected_at is None:
            self._paged_config_rejection = None
            return
        if rejected_at == self._paged_config_rejection:
            return
        self._paged_config_rejection = rejected_at
        if self._operator_alerter is None:
            return
        self._operator_alerter.page(
            rule="rules_config_rejected",
            severity="CRITICAL",
            message=(
                f"The rules config was rejected and is NOT in use: "
                f"{self._rules.validation_error}. Detection is running on the settings that "
                f"validated plus built-in defaults for the rest, so the thresholds in force "
                f"are not the ones in the file. Fix it and the next edit is picked up without "
                f"a restart; `python -m guardian.validate_config <path>` checks a file before you ship it."
            ),
            evidence={
                "problems": self._rules.validation_error,
                "rejected_at": rejected_at,
            },
            dedup_scope=rejected_at,
        )

    def _claim(self, account_id: int, kind: str) -> bool:
        """Reserves this account for this kind of poll. False if one is already
        queued or running."""
        with self._inflight_lock:
            key = (account_id, kind)
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def _release(self, account_id: int, kind: str) -> None:
        """Always called from a `finally`. A claim leaked by a raising poll
        would stop that account being checked for the lifetime of the process -
        a silent, permanent monitoring gap for one customer, which is worse
        than the double-poll this guard exists to prevent."""
        with self._inflight_lock:
            self._inflight.discard((account_id, kind))

    # --- workers -----------------------------------------------------------

    def _fetch_account(self, account_id: int):
        with self._db.cursor() as cur:
            cur.execute("SELECT * FROM guardian_accounts WHERE id = %s AND status = 'active'", (account_id,))
            return cur.fetchone()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                delay = task.due_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                breaker = self.breakers.for_account(task.account_id)
                if not breaker.allow():
                    logger.info("breaker open, skipping account %d", task.account_id)
                    continue
                row = self._fetch_account(task.account_id)
                if row is None:
                    continue
                before_failures = self._consecutive_failures(task.account_id)
                if task.kind == "fast":
                    self._poller.run_fast_loop(row)
                else:
                    self._poller.run_slow_loop(row)
                if self._consecutive_failures(task.account_id) > before_failures:
                    breaker.record_failure()
                else:
                    breaker.record_success()
            except Exception as exc:  # noqa: BLE001 - one task must never kill a worker
                logger.error("task failed account=%d kind=%s: %s", task.account_id, task.kind, exc)
                # Guarded in its own right: this runs *because* something already
                # went wrong, and a failure here would escape the handler that
                # exists to stop one bad account killing a worker. A pool that
                # silently loses workers one at a time ends up polling nothing.
                try:
                    self.breakers.for_account(task.account_id).record_failure()
                except Exception as breaker_exc:  # noqa: BLE001
                    logger.error("breaker update failed for account %d: %s", task.account_id, breaker_exc)
            finally:
                try:
                    self._release(task.account_id, task.kind)
                finally:
                    # Last statement in the finally, so a failure above cannot
                    # leave the queue's unfinished-task count wrong forever.
                    self._queue.task_done()

    def _consecutive_failures(self, account_id: int) -> int:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT consecutive_failures + consecutive_auth_failures AS total "
                "FROM guardian_poll_state WHERE account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
        return row["total"] if row else 0

    # --- lifecycle -----------------------------------------------------------

    def run(self, pool_size: int | None = None) -> None:
        size = pool_size or self._rules.get_int("workers", "pool_size", default=8)
        workers = [threading.Thread(target=self._worker_loop, daemon=True, name=f"worker-{i}") for i in range(size)]
        for w in workers:
            w.start()
        self._workers = workers
        # Kept, so `join()` can wait for it. The scheduler thread runs
        # escalation and the config health check; a SIGTERM whose join waited
        # for the pool alone could return - after the final "stopping"
        # heartbeat - while this thread was still mid-escalation.
        self._scheduler = threading.Thread(target=self._scheduler_loop, daemon=True, name="scheduler")
        self._scheduler.start()
        logger.info("engine running: %d workers", size)
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def run_one_cycle(self) -> None:
        """Synchronous single pass (used by tests and the soak harness)."""
        self._schedule_cycle()
        self._drain_once()

    def _drain_once(self) -> None:
        """Processes everything currently queued, on this thread.

        Split out of run_one_cycle so a test can drive the worker body against
        a task it queued itself - notably to prove that a poll which raises
        still releases its in-flight claim."""
        while not self._queue.empty():
            task = self._queue.get()
            try:
                breaker = self.breakers.for_account(task.account_id)
                if not breaker.allow():
                    continue
                row = self._fetch_account(task.account_id)
                if row is None:
                    continue
                before_failures = self._consecutive_failures(task.account_id)
                if task.kind == "fast":
                    self._poller.run_fast_loop(row)
                else:
                    self._poller.run_slow_loop(row)
                if self._consecutive_failures(task.account_id) > before_failures:
                    breaker.record_failure()
                else:
                    breaker.record_success()
            finally:
                self._release(task.account_id, task.kind)
                self._queue.task_done()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 30.0) -> None:
        """Wait for in-flight polls, and the scheduler pass in progress, to
        finish after `stop()`, up to `timeout` in total - so a SIGTERM lets a
        half-done poll record its state and a half-sent escalation go out."""
        deadline = time.monotonic() + timeout
        threads = list(self._workers)
        if self._scheduler is not None:
            threads.append(self._scheduler)
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if t.is_alive():
                t.join(remaining)
