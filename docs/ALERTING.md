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

## How alerts reach you

`GET /api/admin/alerts` (the Alerts tab of the admin console, refreshed every
30 s) is the live view. Without opening it you still hear about critical
conditions: the `admin_alert_digest` scheduler job runs every 15 minutes and
emails `SUPPORT_EMAIL` plus every admin account when an alert is critical, at
most once per condition every 6 hours (Redis `autobuy:alerts:emailed:<name>`);
it logs at CRITICAL as well. Slack (`SLACK_WEBHOOK_URL`, now passed through
docker-compose) adds real-time lines: one per scheduler run for failed buy
attempts, one per stopped purchase, one per needs_review, one per exchange
withdrawal failure.

Customer emails on failed buys: none on attempts 1 and 2 (they retry minutes
later); one "Purchase missed" email when a cycle's 3 attempts are exhausted,
naming the next attempt time; one distinct "purchase has been stopped" email
when the purchase stops. A failed one-time purchase reaches the stop within
about 15 minutes, so it hears the same day.

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
| Fill deviations | admin alert `fill_deviations` (Redis `autobuy:fill:deviations:<day>`) | warning at 3 / day, critical at 10 | Successful buys deviating > FILL_WARN_PCT from expected: a steady few-percent shortfall; see `GET /api/admin/fills/deviations`, compare with Bitget's fee and slippage |
| Stopped purchases | admin alert `stopped_purchases`; Slack "PURCHASE STOPPED" | critical | A purchase failed 3 attempts x 3 cycles (or a one-time purchase 3 attempts) and no longer runs; re-arm with force-retry after fixing the cause, or the customer recreates it |
| Missed cycles | admin alert `missed_cycles`; Slack "cycle MISSED" | warning | Recurring purchases that missed a whole cycle in the last day and moved on |
| Purchases needing review | admin alert `purchases_needing_review`; Slack "NEEDS REVIEW" | critical | Buy outcome unknown or fill implausible: resolve via the admin Review tab (record fill / mark not sent / cancel) — they never retry on their own |
| Exchange unreachable | admin alert `exchange_unreachable` (failed buy attempts in the last hour whose error is a DNS / timeout / connection failure) | warning at 2, critical at 5 | The runs happened but no order could be placed: missed buys. The customer gets a "Purchase failed … no money was spent" email per attempt and the purchase retries on the next run (3 attempts); check container DNS (`docker exec autobuy_api getent hosts api.bitget.com`) |
| Email delivery failures | admin alert `email_delivery_failures` (Redis `autobuy:email:failures:<hour>`), Slack `email delivery: N emails could not be delivered this hour` | critical | N emails exhausted their retries (30 s … 8 min backoff, 4 attempts for confirmations, 6 for failure emails) in the current hour — Resend down or key/domain broken; list them at `GET /api/admin/emails/undelivered`; purchases are unaffected |
| Email outbox backlog | `autobuy.email_outbox` log line `drained ... remaining > 0` on consecutive runs (Redis list `autobuy:email:outbox`) | warning | Purchase-path emails are queued, not sent inline (`api/services/email_outbox.py`); the scheduler drains after each run and every minute with a 20 s budget and 10 s per email. A growing backlog means Resend is slow or failing; purchases are unaffected. Timed-out or rejected emails are logged and dropped, never retried |
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
