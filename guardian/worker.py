"""The Guardian worker: `python -m guardian.worker`.

One process, the engine's scheduler thread plus its worker pool, polling
every active monitored account on the platform's Postgres and delivering
through the platform's transports. Started by the `guardian` compose service
on the API image; no ports, no HTTP of its own except the Prometheus
exposition on the compose network.

With GUARDIAN_ENABLED off it does nothing but write its heartbeat, so a
deploy can prove the container runs before anything is switched on, and
switching on is a `.env` change plus a restart, not a code change.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time

logger = logging.getLogger("guardian.worker")

IDLE_BEAT_SECONDS = 60


def _commit() -> str:
    try:
        with open("/app/GIT_COMMIT") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _setup_logging() -> None:
    """The API's JSON log format, so the post-deploy WARNING/ERROR scan
    reads the worker the same way."""
    try:
        from api.services.logger import setup_logging
        setup_logging()
    except Exception:  # noqa: BLE001 - logging must come up even if the platform helper changes
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class _Hooks:
    """What the engine calls each scheduler pass (see Engine._scheduler_tick)."""

    def __init__(self, heartbeat, digest):
        self._heartbeat = heartbeat
        self._digest = digest

    def paused(self) -> bool:
        return self._heartbeat.paused()

    def beat(self, snapshot: dict) -> None:
        self._heartbeat.beat(snapshot)

    def after_tick(self, paused: bool) -> None:
        if not paused:
            self._digest.run_if_due()


def _idle(heartbeat, stop: threading.Event) -> int:
    logger.info("GUARDIAN_ENABLED is off: worker idle, heartbeat only")
    while not stop.is_set():
        heartbeat.beat({"idle": True, "paused": False, "active_accounts": 0})
        stop.wait(IDLE_BEAT_SECONDS)
    heartbeat.stopping()
    return 0


def main() -> int:
    _setup_logging()
    from api.config import settings
    from guardian.heartbeat import Heartbeat

    stop = threading.Event()
    heartbeat = Heartbeat(settings.redis_url, enabled=settings.guardian_enabled, commit=_commit())

    def _on_signal(signum, _frame):
        logger.info("signal %s: stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if not settings.guardian_enabled:
        return _idle(heartbeat, stop)

    from guardian.alerts.contact import ContactRouter
    from guardian.alerts.escalation import Escalator
    from guardian.alerts.operator import OperatorAlerter
    from guardian.channels import build_channels, build_operator_channels, digest_channels
    from guardian.config import RulesConfig
    from guardian.crypto import CredentialCipher
    from guardian.db import Database
    from guardian.digest_job import DailyDigest
    from guardian.engine import Engine
    from guardian.events import record_config_changes
    from guardian.metrics import Metrics

    db = Database()
    cipher = CredentialCipher()
    rules = RulesConfig(settings.guardian_rules_path, on_change=lambda changes: record_config_changes(db, changes))
    routing_problems: list = []
    channels = build_channels(rules, problems=routing_problems, db=db)

    metrics = Metrics()
    metrics.serve(settings.guardian_metrics_port, host=settings.guardian_metrics_host)

    operator_alerter = OperatorAlerter(db, build_operator_channels(rules, db=db), rules)
    for problem in routing_problems:
        if problem["kind"] == "fallback":
            operator_alerter.page(
                rule="alert_routing_fell_back_to_defaults", severity="HIGH",
                message=(f"No usable alert routing is configured for {problem['severity']}, so the built-in "
                         f"defaults are in use. Alerts of that severity are going wherever the defaults point, "
                         f"which is not a choice anyone made for this deployment."),
                evidence=problem, dedup_scope=f"routing-fallback:{problem['severity']}")
        else:
            operator_alerter.page(
                rule="alert_channel_routed_but_not_configured", severity="HIGH",
                message=(f"Channel '{problem['channel']}' is routed for {problem['severity']} alerts but has no "
                         f"configuration, so it delivers nothing. Every {problem['severity']} alert is "
                         f"under-delivered for as long as this process runs. Check the platform settings for "
                         f"that channel."),
                evidence=problem, dedup_scope=f"{problem['channel']}:{problem['severity']}")

    contact_router = ContactRouter(db=db, rules=rules, operator_alerter=operator_alerter)
    escalation_minutes = rules.get_int("alerting", "escalation_unacknowledged_minutes", default=15)
    escalator = Escalator(channels.get("CRITICAL", []), escalation_minutes,
                          operator_alerter=operator_alerter, contact_router=contact_router)
    digest = DailyDigest(db, lambda: digest_channels(db),
                         hour_utc=rules.get_int("alerting", "digest_hour_utc", default=8),
                         redis_url=settings.redis_url)
    engine = Engine(db, cipher, rules, channels, metrics=metrics, escalator=escalator,
                    operator_alerter=operator_alerter, contact_router=contact_router,
                    hooks=_Hooks(heartbeat, digest))

    logger.info("guardian worker starting: commit %s, rules %s", _commit(), settings.guardian_rules_path)
    runner = threading.Thread(target=engine.run, name="engine", daemon=True)
    runner.start()
    while not stop.is_set() and runner.is_alive():
        stop.wait(1.0)
    engine.stop()
    engine.join(30.0)
    heartbeat.stopping()
    db.close()
    logger.info("guardian worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
