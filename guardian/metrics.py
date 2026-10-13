from __future__ import annotations

import logging
import threading

logger = logging.getLogger("guardian.metrics")

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
    from prometheus_client.exposition import CONTENT_TYPE_LATEST

    _AVAILABLE = True
except ImportError:  # metrics are strictly optional - never a hard dependency at runtime
    _AVAILABLE = False


class Metrics:
    """Prometheus counters/gauges. A no-op shell when prometheus_client
    isn't installed, so callers never need to null-check."""

    def __init__(self, registry=None):
        # Maintained regardless of _AVAILABLE - see untimestamped_record.
        self._undated: dict = {}
        self._unidentifiable = 0
        self._undated_lock = threading.Lock()
        if not _AVAILABLE:
            self.registry = None
            return
        # Never the process-wide default REGISTRY: the API has its own metrics
        # module on it, and a worker and an API that both import this must not
        # register the same collectors twice.
        reg = CollectorRegistry() if registry is None else registry
        self.registry = reg
        self.polls_ok = Counter("guardian_polls_ok_total", "Successful poll cycles", registry=reg)
        self.polls_failed = Counter("guardian_polls_failed_total", "Failed poll cycles", ["kind"], registry=reg)
        self.detections = Counter(
            "guardian_detections_total", "Detections fired", ["rule", "severity"], registry=reg
        )
        self.alerts = Counter("guardian_alerts_sent_total", "Alerts sent (post-dedup)", ["severity"], registry=reg)
        self.accounts_active = Gauge("guardian_accounts_active", "Accounts with status=active", registry=reg)
        # A customer said an alert was their own activity (G-24, #28): a
        # confirmed false positive, stated by the only person who can know.
        #
        # Deliberately a count of confirmed-wrong and NOT a precision ratio.
        # An unmarked alert is unjudged, not correct, and most alerts are never
        # judged by anybody - so dividing marks by all alerts would let a quiet
        # week of nobody looking read as a detector improving. Rate this
        # against `sentinel_detections_total` for the same rule when you want a
        # trend, and read the denominator on the dashboard.
        self.marked_expected = Counter(
            "guardian_alerts_marked_expected_total",
            "Alerts a customer marked as their own activity (confirmed false positives)",
            ["rule"],
            registry=reg,
        )
        # A record the exchange sent without a usable timestamp. Rising here
        # means an exchange changed its response format: those records can no
        # longer be placed in time, so quantity-drop stops crediting them as
        # explanations (G-7) and off-market/fee checking stops seeing them at
        # all (#51). Both failures are silent, which is exactly why this is a
        # counter an operator can alarm on and not only a log line.
        self.untimestamped_records = Counter(
            "guardian_untimestamped_records_total",
            "Exchange records received with a missing or unparseable timestamp",
            ["exchange", "source"],
            registry=reg,
        )
        # A withdrawal the exchange sent with neither a reference nor a time.
        # Those cannot be told apart from each other, so all but the first are
        # dropped as already seen - a MISSED CRITICAL on the money-leaving
        # path, and previously an entirely silent one (#67). This counter is
        # what makes the real frequency knowable, which is what the eventual
        # fix has to be chosen from.
        self.unidentifiable_withdrawals = Counter(
            "guardian_unidentifiable_withdrawals_total",
            "Withdrawals received with neither an exchange reference nor a timestamp",
            ["exchange"],
            registry=reg,
        )
        # 0 means config/rules.yaml on disk was rejected and detection is
        # running on the last values known to be good (G-12). Its resting value
        # is 1, so this is a flat line an operator can alarm on: the thresholds
        # someone just edited are not the thresholds in force, and every other
        # indicator - poll success, account health, alert volume - looks
        # completely normal while that is true.
        self.rules_config_valid = Gauge(
            "guardian_rules_config_valid",
            "1 when config/rules.yaml validated and is in use, 0 when it was rejected",
            registry=reg,
        )
        self.rules_config_valid.set(1)
        # Row counts per table. This project has already had one outage from a
        # query going quadratic over a table nobody was watching grow, so the
        # size is a first-class signal rather than something to discover.
        self.table_rows = Gauge(
            "guardian_table_rows", "Rows per table", ["table"], registry=reg,
        )

    def set_table_rows(self, counts: dict) -> None:
        if _AVAILABLE:
            for table, rows in counts.items():
                self.table_rows.labels(table=table).set(rows)

    def set_rules_config_valid(self, ok: bool) -> None:
        if _AVAILABLE:
            self.rules_config_valid.set(1 if ok else 0)

    def poll_ok(self) -> None:
        if _AVAILABLE:
            self.polls_ok.inc()

    def poll_failed(self, auth: bool) -> None:
        if _AVAILABLE:
            self.polls_failed.labels(kind="auth" if auth else "generic").inc()

    def detection_fired(self, rule: str, severity: str) -> None:
        if _AVAILABLE:
            self.detections.labels(rule=rule, severity=severity).inc()

    def alert_sent(self, severity: str) -> None:
        if _AVAILABLE:
            self.alerts.labels(severity=severity).inc()

    def alert_marked_expected(self, rule: str) -> None:
        if _AVAILABLE:
            self.marked_expected.labels(rule=rule).inc()

    def set_active_accounts(self, count: int) -> None:
        if _AVAILABLE:
            self.accounts_active.set(count)

    def untimestamped_record(self, exchange: str, source: str) -> None:
        # Counted twice on purpose. The Prometheus counter is for scraping; the
        # plain dict is what `alarms.UntimestampedRecordAlarm` reads, and it is
        # maintained whether or not prometheus_client is installed. This class
        # is explicitly a no-op shell without it, and an alarm that quietly
        # stops existing along with an optional dependency is worse than no
        # alarm - it looks like coverage.
        with self._undated_lock:
            self._undated[(exchange, source)] = self._undated.get((exchange, source), 0) + 1
        if _AVAILABLE:
            self.untimestamped_records.labels(exchange=exchange, source=source).inc()

    def unidentifiable_withdrawal(self, exchange: str) -> None:
        # Same two-place counting as untimestamped_record, and for the same
        # reason: the plain integer survives prometheus_client being absent,
        # and a count that quietly stops existing with an optional dependency
        # looks exactly like a count of zero.
        with self._undated_lock:
            self._unidentifiable += 1
        if _AVAILABLE:
            self.unidentifiable_withdrawals.labels(exchange=exchange).inc()

    def unidentifiable_withdrawals_total(self) -> int:
        with self._undated_lock:
            return self._unidentifiable

    def untimestamped_snapshot(self) -> dict:
        """Cumulative undated-record counts per (exchange, source). A copy, so
        a caller diffing two snapshots isn't reading a live dict."""
        with self._undated_lock:
            return dict(self._undated)

    def serve(self, port: int, host: str = "127.0.0.1"):
        """Raw Prometheus exposition for scrapers, on the compose network only
        (no port is published). Returns the server, or None when
        prometheus_client isn't installed, so the bind can be asserted."""
        if not _AVAILABLE or self.registry is None:
            logger.warning("prometheus_client not installed - metrics endpoint disabled")
            return None
        server = _serve_metrics(self.registry, port, host)
        logger.info("prometheus metrics on %s:%d", host, port)
        return server

    def exposition(self) -> tuple[str, bytes]:
        """(content-type, body) of the raw exposition."""
        if not _AVAILABLE or self.registry is None:
            return "text/plain; charset=utf-8", b"# prometheus_client not installed\n"
        return CONTENT_TYPE_LATEST, generate_latest(self.registry)


def _serve_metrics(registry, port: int, host: str = "127.0.0.1"):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = generate_latest(registry)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep stdout JSON-clean
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="metrics").start()
    return server
