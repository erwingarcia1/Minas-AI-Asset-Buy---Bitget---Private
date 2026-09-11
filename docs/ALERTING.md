# Alerting Thresholds & Runbook

Metrics are exported at `GET /metrics` (Prometheus text format). Scrape every
15–30s. The app also self-reports one critical condition in logs: **5
consecutive failed purchase executions** emits a `CRITICAL` log line from
`autobuy.tasks.purchases` (and the `scheduler_consecutive_failures` gauge tracks it).

The Auto-Buy `execute_purchases` job runs every 5 minutes in every API worker
(`autobuy.tasks.purchases` logger, `autobuy_*` admin alerts, one worker wins the
advisory lock per run). The admin pause switch lives in Redis
(`autobuy:schedulers:paused`), the job reports a heartbeat there, and
`GET /health` → `schedulers.autobuy` shows it, whichever worker answers.

## Alert rules

| Condition | Query sketch | Severity | Likely cause / first action |
|---|---|---|---|
| Request latency p95 > 2s (5m) | `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` | page | DB slowness or worker starvation — check slow-query WARNINGs, worker count |
| HTTP 5xx rate > 1% (5m) | `rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])` | page | Check `autobuy.errors` logs by request_id |
| Auto-Buy consecutive failed runs ≥ 5 | `scheduler_consecutive_failures >= 5` (gauge fed by execute_purchases) | critical | Exchange down/keys invalid/withdrawals blocked — see the CRITICAL log and Slack alert; due purchases retry on the next 5-minute run, up to 3 attempts each |
| Binance errors > 10/h | `increase(binance_api_calls_total{result="error"}[1h]) > 10` | alert | Rate-limit ban or API change; check error_message on failed transactions |
| Email failure ratio > 20% (1h) | `increase(emails_total{result="failed"}[1h]) / increase(emails_total[1h])` | alert | Resend outage or bad key; sends are fire-and-forget so trades are unaffected |
| Active purchases = 0 | `active_purchases_total == 0` (set each execute_purchases run) | warning | Legitimate if no users; otherwise the job may be stuck — check `/health` `schedulers.autobuy` |
| Auto-Buy run overdue | `/health` `schedulers.autobuy.last_run_at` older than 12m (2 intervals + misfire grace) while not paused; admin alert `autobuy_run` | critical | execute_purchases not running in any worker — check `autobuy.tasks.purchases` logs for DISCARDED/SKIPPED windows, restart |
| Auto-Buy consecutive failures ≥ 5 | admin alert `autobuy_consecutive_failures` (`/health` `schedulers.autobuy.consecutive_failures`) | critical | Every due purchase failed on 5 runs in a row — exchange down, keys invalid, withdrawals blocked; see `last_error` on the purchases |
| Withdrawal poll overdue | `/health` `schedulers.withdrawals.last_run_at` older than 25m while not paused | warning | `poll_withdrawals` (every 10 min) confirms sent withdrawals with Bitget; sent coins are unaffected, but tx hashes and exchange-side failures stop being recorded — check the `autobuy.tasks.purchases` logs |
| Withdrawal rejected by the exchange | Slack `AutoBuy withdrawal failed` after a poll (`schedulers.withdrawals.last_counts.failed > 0`) | critical | The buy filled, the coins sit on the exchange and the purchase is `withdrawal_failed`; fix the cause (permissions, IP whitelist, address book, chain) and use "Retry withdrawal" — never re-buy |
| Withdrawal unconfirmed after 7 days | Slack `still unconfirmed after 7 days` (row `raw_status = unconfirmed`) | warning | Bitget never reported the withdrawal — check it in the Bitget withdrawal history by client id / withdrawal id |
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

`failed_transactions_24h` (admin alerts) counts failed Auto-Buy execution attempts in `purchase_transactions`.
