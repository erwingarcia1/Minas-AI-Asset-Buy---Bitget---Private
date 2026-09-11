# Alerting Thresholds & Runbook

Metrics are exported at `GET /metrics` (Prometheus text format). Scrape every
15–30s. The app also self-reports one critical condition in logs: **5
consecutive failed purchase executions** emits a `CRITICAL` log line from
`autobuy.scheduler` (and the `scheduler_consecutive_failures` gauge tracks it).

Two schedulers run in every API worker: the legacy recurring tick (`autobuy.scheduler`
logger, `scheduler_*` alerts) and the Auto-Buy `execute_purchases` job every 5 minutes
(`autobuy.tasks.purchases` logger, `autobuy_*` alerts). Both share one admin pause
switch stored in Redis (`autobuy:schedulers:paused`), report a heartbeat there, and
`GET /health` → `schedulers` shows both, whichever worker answers.

## Alert rules

| Condition | Query sketch | Severity | Likely cause / first action |
|---|---|---|---|
| Request latency p95 > 2s (5m) | `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` | page | DB slowness or worker starvation — check slow-query WARNINGs, worker count |
| HTTP 5xx rate > 1% (5m) | `rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])` | page | Check `autobuy.errors` logs by request_id |
| Scheduler consecutive failures ≥ 5 | `scheduler_consecutive_failures >= 5` | critical | Binance down/keys invalid/balances empty — see CRITICAL log; executions retry hourly |
| Binance errors > 10/h | `increase(binance_api_calls_total{result="error"}[1h]) > 10` | alert | Rate-limit ban or API change; check error_message on failed transactions |
| Email failure ratio > 20% (1h) | `increase(emails_total{result="failed"}[1h]) / increase(emails_total[1h])` | alert | Resend outage or bad key; sends are fire-and-forget so trades are unaffected |
| Active purchases = 0 | `active_purchases_total == 0` | warning | Legitimate if no users; otherwise scheduler may be stuck — check `/health` `last_tick_at` |
| Scheduler tick stalled | `/health` `schedulers.recurring.last_run_at` older than 5m | critical | App wedged or lock never released — restart; the advisory lock frees on disconnect |
| Auto-Buy run overdue | `/health` `schedulers.autobuy.last_run_at` older than 12m (2 intervals + misfire grace) while not paused; admin alert `autobuy_run` | critical | execute_purchases not running in any worker — check `autobuy.tasks.purchases` logs for DISCARDED/SKIPPED windows, restart |
| Auto-Buy consecutive failures ≥ 5 | admin alert `autobuy_consecutive_failures` (`/health` `schedulers.autobuy.consecutive_failures`) | critical | Every due purchase failed on 5 runs in a row — exchange down, keys invalid, withdrawals blocked; see `last_error` on the purchases |
| Schedulers paused | admin alert `schedulers_paused` = warning; `/health` `schedulers.paused` = true | warning | Someone pressed the admin emergency stop (`POST /api/admin/scheduler/pause`); both schedulers skip every run until `POST /api/admin/scheduler/resume`. `paused` = null means Redis is unreadable: both schedulers fail closed |

## Runbook

1. **Grab the request id** from the alerting sample or the 500 body and grep the
   JSON logs (`request_id` field) for the full trace.
2. **Scheduler issues**: `GET /health` shows `running`, `dry_run`, and
   `last_tick_at`. A restart is safe: due purchases are idempotent per due-date
   and a missed window buys once (no catch-up spree).
3. **Binance issues**: failed transactions carry the normalized reason in
   `error_message`. Key problems → have the user re-verify
   (`POST /api/exchange/keys/{id}/verify` gives a granular verdict).
4. **Emergency stop**: set `SCHEDULER_ENABLED=0` (or `AUTOBUY_DRY_RUN=1` to keep
   scheduling but simulate) and restart. Both are read at startup.
5. **Database**: schema is Alembic-managed; never run `create_all` in
   production. `alembic current` shows the applied revision.
