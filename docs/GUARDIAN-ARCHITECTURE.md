# Guardian — architecture inside the platform

Guardian is the read-only exchange-monitoring half of the product (the other
half is AutoBuy). It watches a customer's Bitget, Bybit, OKX or Binance
account for the signs of a compromise — withdrawals to addresses never used,
balances falling by more than activity explains, trading in markets the
account never touched, the monitoring key itself being rejected — and tells
the customer within a minute of the exchange reporting it. It detects and
tells; it cannot stop anything, and it never holds a key that could.

This is the current-state architecture after the merge (v0.9.1). Guardian's
original design document, written for its standalone service, is the source
for the engine's internals; what changed is where it runs and what it talks
to.

## System overview

```
browser ── caddy ── nginx (web) ── api (gunicorn x4, FastAPI)
                                    │  /api/guardian/*        member pages (PR4)
                                    │  /api/admin/guardian/*  fleet, triage, onboarding (PR5)
                                    │  /health.guardian       the worker's heartbeat, read from Redis
                                    │
                                    ├── Postgres: users, api_keys, purchases …  +  guardian_* (19 tables, migration 030)
                                    ├── Redis:    email outbox · autobuy:guardian:{heartbeat, paused, digest:last_date}
                                    │
                                    └── guardian worker (container autobuy_guardian, same image, no ports)
                                          python -m guardian.worker
                                          ┌─────────────────────────────────────────────────────────┐
                                          │ scheduler thread ──► queue ──► 8 worker threads          │
                                          │   every pass: pause switch · heartbeat · retention ·     │
                                          │   escalation · config health · daily digest (08:00 UTC)  │
                                          │ poller: fast loop 45 s (withdrawals, deposits, transfers)│
                                          │         slow loop 600 s (balances, permissions, uid)     │
                                          │ detectors/ (12 rules) ──► dispatch (dedup, cooldowns)    │
                                          └──────────────┬──────────────────────────┬───────────────┘
                                                         │ read-only ccxt calls       │ alerts
                                                         ▼                            ▼
                                             Bitget / Bybit / OKX / Binance    Resend outbox (customer + admin email)
                                                                                api/services/telegram (ops chat, team chat)
                                                                                ClickSend / Twilio SMS
```

## What moved, what stayed

| Guardian (standalone) | Platform (v0.9.1) |
|---|---|
| `sentinel/` package | `guardian/` package, same modules, `sentinel.` → `guardian.` |
| SQLite file, self-migrating `ALTER` list | Postgres, `guardian_*` tables created by Alembic migration 030; the worker's raw-SQL wrapper (`guardian/db.py`) keeps the `cursor()` contract on psycopg2 with typecasters so the engine SQL is unchanged |
| `accounts.alert_email` names the customer | `guardian_accounts.user_id` → `users.id`; the owner's `users.email` is the primary alert destination (`guardian/contacts.py`) |
| `SENTINEL_MASTER_KEY` raw Fernet | the platform cipher (`api/services/crypto.py`, sha256 of `ENCRYPTION_KEY`), rotated by `scripts/rotate_encryption_key.py` |
| SMTP email | the Resend outbox (`api/services/email_outbox.enqueue_sync`), drained by the API every minute |
| own Telegram sender | `api/services/telegram.send_sync` (token redaction, rate limit); emails masked |
| host cron `sentinel digest` | `guardian/digest_job.py` inside the worker, once a day after `alerting.digest_hour_utc` |
| ops dashboard + member portal (HTML) | React pages in the SPA, `/api/guardian/*` and `/api/admin/guardian/*` (PR4, PR5) |
| operators table, portal members, scrypt, HMAC cookies | platform users and JWT sessions; operators are `role=admin`; TOTP is the platform's `user_totp` (migration 029) |
| `config/rules.yaml` bind-mounted, hot reload | `guardian/rules.yaml` baked into the image, `GUARDIAN_RULES_PATH` optional override, hot reload kept |
| `SENTINEL_*` environment | `api.config.settings` (`GUARDIAN_*`, `CLICKSEND_*`, `TWILIO_TO_NUMBERS`); no `os.getenv` in `guardian/` |

Unchanged: the engine (`engine.py`), the poller, the twelve detectors, the
dispatch/dedup/cooldown logic, escalation of unacknowledged CRITICALs,
retention, the rate limiters and circuit breakers, the per-exchange read-only
gates in `onboarding.py`, the operator pager, and the customer email copy.

## Components (`guardian/`)

- **`worker.py`** — the container's process. With `GUARDIAN_ENABLED=0` it only
  writes its heartbeat every 60 s; otherwise it builds the database wrapper,
  the cipher, the rules, the channels, the operator pager, the contact router,
  the escalator and the engine, serves the Prometheus exposition on the compose
  network, and stops cleanly on SIGTERM (polls in flight get 30 s).
- **`engine.py`** — scheduler → queue → worker pool, per-account in-flight
  claims, breakers and limiters. Each pass consults the hooks: the pause switch
  (fail closed: unreadable Redis = no polls this pass), the heartbeat, and
  after-tick work (the digest).
- **`heartbeat.py`** — `autobuy:guardian:heartbeat` (JSON, TTL 180 s) and the
  pause switch `autobuy:guardian:paused`; also touches `/tmp/guardian.heartbeat`
  for compose's healthcheck. The API reads the same keys
  (`api/services/guardian_state.py`) for `/health` and the admin pause/resume.
- **`db.py`** — psycopg2, thread-local connections, one transaction per
  `cursor()`; timestamps come back as ISO text and jsonb as text, the shapes
  the engine wrote; `lastrowid` is kept for plain inserts.
- **`contacts.py`** — the only place that joins `users`: the owner's email and
  verified phone plus the account's own overrides; `accounts_for_user` is the
  isolation boundary for anything per-user.
- **`channels.py`** — the named channels (`telegram`, `telegram_team`, `sms`,
  `email` to every admin) and the severity routing from `rules.yaml`.
- **`alerts/`** — `base.py` (messages, runbook, the `/guardian` link from
  `APP_BASE_URL`), `dispatch.py`, `contact.py`, `escalation.py`, `digest.py`,
  `operator.py`, and the adapters `email.py`, `telegram.py`, `sms.py`,
  `clicksend.py`. Every outbound socket is `guardian/net.py`, which refuses to
  connect under test.
- **`onboarding.py`** — the fail-closed read-only gate per exchange and
  `onboard_account(user_id=…, source=…)`, which stores the key's fingerprint and
  the owner and seeds the baseline.
- **`metrics.py`** — Prometheus counters and gauges on the worker's own
  registry (`guardian_*` names), raw exposition on `GUARDIAN_METRICS_PORT`.

## Data

Nineteen `guardian_*` tables (migration 030): `guardian_accounts` (owner,
label, exchange, topology, encrypted key material, key fingerprint, contact
overrides, status, exchange uid, baseline status, onboarding source),
`guardian_poll_state` (failure counters, last loops, breaker state),
`guardian_events` (append-only forensic log), the baselines
(`known_addresses`, `traded_pairs`, `transfer_routes`, `fee_baseline`), the
dedup tables (`seen_withdrawals`, `seen_fills`, `seen_transfers`), the
snapshots (`balance_snapshots`, `permission_snapshots`), and the alerting
tables (`alerts`, `snoozes`, `expected_activity`, `alert_triage`,
`sms_deliveries`, `operator_pages`, `config_changes`). Ids are bigint identity
so Guardian's dedup keys survive the cutover with their original account ids.
`api/models/guardian.py` mirrors the schema for the API; a test asserts the
mirror and the migration agree.

## Isolation from trading

Nothing in `guardian/` imports a trading transport, and nothing reaches an
order, withdrawal or transfer endpoint; the trading jobs never read a
`guardian_*` table; there is no foreign key between `guardian_*` and
`api_keys`. `tests/test_guardian_isolation.py` (PR3) enforces it. The
monitoring key is a separate read-only key: a purchase key is refused by its
fingerprint before the gate even runs.
