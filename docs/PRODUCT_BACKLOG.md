# AutoBuy Crypto Platform — Product Status & MVP Backlog

> **Superseded for issue tracking:** the issue list in §4 has been reorganized into
> [`GITHUB_ISSUES.md`](../GITHUB_ISSUES.md) (canonical, 24 issues, 6 phases) with the
> timeline in [`ROADMAP.md`](../ROADMAP.md). §1–§3 analysis here remains current.

*Prepared 2026-09-01 · Covers the FastAPI platform in `api/` (Phase 1 complete).*

> **Repo context:** this repository hosts two systems. The **legacy sub-account
> auto-buy bot** (`autobuy.py`, four exchange clients, Flask `webapp/`, meme-buy — removed from the repo on 2026-09-22, last present at tag v0.6.5-pre-audit;
> subsystem) is mature and documented in the README. The **AutoBuy Crypto
> Platform** (`api/`) is a new multi-tenant SaaS-style backend being built
> alongside it. This document covers the platform only.

---

## 1. Current State Analysis

### Data model (`api/models.py`) — 4 tables, PostgreSQL, UUID PKs

| Table | Purpose | Notable columns |
|---|---|---|
| `users` | Accounts | `email` (unique), `password_hash` (Argon2), `kyc_status` (pending/verified/rejected), `kyc_sumsub_id` |
| `api_keys` | Exchange credentials | `exchange`, `encrypted_api_key` / `encrypted_api_secret` (Fernet), `verified` flag |
| `recurring_purchases` | DCA schedules | `crypto`, `amount_usdt`, `frequency`, `next_purchase_date`, `withdrawal_address`, `enabled` |
| `transactions` | Purchase history | `amount_crypto`, `cost_usdt`, `binance_order_id`, `status` (pending/completed/failed), `error_message` |

All relationships cascade-delete from `users`. `recurring_purchases → transactions`
also cascade-deletes (see tech-debt note in §3).

### API surface (`api/routes/`) — 26 endpoints across 6 routers

| Router (prefix) | Endpoints |
|---|---|
| `auth` (`/api/auth`) | `POST /register`, `/login`, `/verify`, `/refresh` |
| `user` (`/api/user`) | `GET/PUT /profile`, `GET /{user_id}` |
| `exchange` (`/api/exchange`) | `POST/GET /keys`, `DELETE /keys/{id}` |
| `purchases` (`/api/purchases`) | `POST/GET /`, `GET/PUT/DELETE /{id}`, `PATCH /{id}/toggle` |
| `transactions` (`/api/transactions`) | `GET /` (paginated), `GET /{id}`, `/stats/summary`, `/crypto/{c}`, `/status/{s}` |
| `dashboard` (`/api/dashboard`) | `GET /`, `/stats`, `/analytics/timeline` |
| `main` | `GET /health`, `GET /` |

### Infrastructure

- **FastAPI 0.104 + SQLAlchemy 2 async + asyncpg**, lifespan-managed engine,
  `create_all` on startup (no migrations yet — Alembic pinned but unused,
  `migrations/versions/` is empty scaffolding).
- **Docker Compose**: Postgres 16 + Redis 7 with healthchecks. **Redis is
  provisioned but nothing uses it yet** (earmark: rate limiting, scheduler locks).
- **Config**: `pydantic-settings` + `.env`; `.env.example` documents all vars.
  Placeholders exist for Resend, Sumsub, and Binance keys — no service code
  behind them yet (`api/services/` is empty).
- **Deps pinned** in `requirements.txt` for phases ahead: `apscheduler`,
  `python-binance`, `aiosmtplib`, `alembic`, `pytest(-asyncio)`, `httpx`,
  `python-json-logger` (resend + sumsub SDKs commented out).

---

## 2. What's Been Built (Phase 1 — complete & smoke-tested)

**Authentication & Authorization** — Argon2 password hashing; JWT access (30 min)
+ refresh (7 d) pairs; refresh tokens carry a `type=refresh` claim and are
rejected as access tokens (and vice versa); `/verify` and `/refresh` endpoints;
shared `get_current_user()` dependency validates the Bearer token and loads the
user row. ✅ Verified end-to-end.

**User Management** — own profile read/update (email with uniqueness check, KYC
status with allow-list validation), public profile by id. KYC status is a
manually-set field for now — no verification flow.

**Exchange Integration (storage only)** — per-user, per-exchange key upsert with
Fernet encryption at rest (key derived from `SECRET_KEY`), masked display
(`AKIA...2345`), delete. `verified` resets on credential change but **nothing
verifies keys against Binance yet**, and no trading code uses them.

**Recurring Purchase Scheduling (CRUD only)** — full CRUD + enable/disable
toggle; frequency validated (`daily/weekly/biweekly/monthly`); first buy
scheduled for tomorrow. **No scheduler executes these yet.**

**Transaction History** — paginated listing with status filter, per-crypto and
per-status views, aggregate stats (invested/average/status counts) in a single
SQL query. **Nothing writes transactions yet** except tests.

**Dashboard & Analytics** — holdings from completed transactions, active +
upcoming (30-day) schedules, monthly investment average, zero-filled daily
timeline (`date_trunc`).

**Cross-cutting** — every query filtered by `user_id` (isolation verified with a
two-user test); timezone-aware UTC responses over naive-UTC columns; proper
400/401/404 usage; 33-check smoke suite passed against live dev Postgres.

---

## 3. Gap Analysis → MVP

The platform currently **stores intent but takes no action**: no purchases
execute, no emails send, no KYC verifies. Phase 2 turns it into a product;
Phase 3 makes it trustworthy; Phase 4 makes it sellable.

Known tech debt to carry into the issues below:

1. Deleting a recurring purchase **cascade-deletes its transaction history**
   (model-level `cascade="all, delete-orphan"`); should be `SET NULL`.
2. `allowed_origins` is hard-coded in `config.py`; the `ALLOWED_ORIGINS` env var
   in `.env.example` is not actually consumed (and a CSV value would crash
   pydantic-settings' JSON parsing if it were).
3. Fernet key is derived from the JWT `SECRET_KEY` — rotating one rotates both.
4. No token revocation/logout; refresh tokens live 7 days irrevocably.
5. `create_all` schema management; needs Alembic before any column changes.
6. DB `DateTime` columns are naive; consider `DateTime(timezone=True)` in the
   first Alembic migration.

---

## 4. GitHub-Ready Issues

Priorities: **P0** = MVP-blocking · **P1** = launch-required · **P2** = fast-follow · **P3** = later.
Estimates: **S** ≤ 1 day · **M** 2–3 days · **L** ~1 week · **XL** > 1 week.

---

### Issue #1 — Purchase Execution Service (Binance spot buy)

**Labels:** `phase-2` `backend` `integration` `P0` · **Estimate:** L

**Description**
Create `api/services/executor.py` that turns a due `RecurringPurchase` into a
real Binance spot market buy. Decrypt the user's stored API key, place a
quote-denominated market order for `amount_usdt` of `crypto`, and record a
`Transaction` (`completed` with `amount_crypto`, `binance_order_id`; or `failed`
with `error_message`). The legacy `binance_client.py` already implements
normalized market-buys, symbol precision flooring, and min-notional handling —
reuse it rather than rewriting against `python-binance`.

**Acceptance criteria**
- [ ] Executes a market buy for a given purchase id; idempotent per due-date (no double buys on retry)
- [ ] Writes a `Transaction` row for every attempt, success or failure
- [ ] Failure taxonomy mapped to `error_message` (insufficient balance, invalid key, min-notional, symbol not found)
- [ ] Skips users whose `kyc_status != verified` and disabled/keyless purchases
- [ ] Testnet/dry-run mode via config flag; unit tests with mocked exchange

**Dependencies:** none (unblocks #2, #3)

---

### Issue #2 — Recurring Job Scheduler (APScheduler)

**Labels:** `phase-2` `backend` `P0` · **Estimate:** M

**Description**
Add `api/services/scheduler.py`: an `AsyncIOScheduler` started in the FastAPI
lifespan that polls for `enabled` purchases with `next_purchase_date <= now`,
invokes the executor (#1), then advances `next_purchase_date` by frequency
(daily +1d, weekly +7d, biweekly +14d, monthly +1 calendar month).

**Acceptance criteria**
- [ ] Due purchases execute within one poll interval (configurable, default 60 s)
- [ ] `next_purchase_date` advances even when execution fails (no hot-loop retry storms); failures follow a capped retry policy
- [ ] Single-instance safety: Redis (already provisioned) or Postgres advisory lock so two API replicas never double-execute
- [ ] Scheduler status exposed on `/health`
- [ ] Tests cover date advancement for all four frequencies incl. month-end edge cases (Jan 31 → Feb 28)

**Dependencies:** #1

---

### Issue #3 — Email Notifications (Resend)

**Labels:** `phase-2` `backend` `integration` `P1` · **Estimate:** M

**Description**
`api/services/email.py` with templated transactional email via Resend
(`RESEND_API_KEY` is already in config; un-comment the SDK in requirements or
call their REST API via httpx). Events: welcome-on-register, purchase executed
(receipt), purchase failed (with reason + fix hint), KYC status change.

**Acceptance criteria**
- [ ] Fire-and-forget sends (background task) — an email failure never fails the triggering request or trade
- [ ] All sends logged; no-op cleanly when `RESEND_API_KEY` is unset (dev)
- [ ] Plain-text + HTML templates for the four events
- [ ] Unsubscribe/notification-preference field on `users` honored for non-critical mail

**Dependencies:** #1 (for receipt events)

---

### Issue #4 — KYC Verification Flow (Sumsub)

**Labels:** `phase-2` `backend` `integration` `compliance` `P0` · **Estimate:** L

**Description**
Replace the manual `kyc_status` toggle with a real flow: `POST /api/kyc/token`
creates a Sumsub applicant (store `kyc_sumsub_id`) and returns an SDK access
token for the frontend widget; `POST /api/kyc/webhook` receives review results
(HMAC-verified) and updates `kyc_status`. Remove `kyc_status` from the
user-writable `PUT /profile` fields — **users must not be able to verify themselves**
(it is currently self-serve, which is a compliance hole the moment real money moves).

**Acceptance criteria**
- [ ] Applicant created lazily on first token request; id persisted
- [ ] Webhook signature verified; `GREEN → verified`, `RED (FINAL) → rejected`
- [ ] `kyc_status` no longer writable via profile update (route + test updated)
- [ ] Purchase execution (#1) and, optionally, purchase creation gated on `verified`
- [ ] Sandbox-mode integration test with mocked webhook payloads

**Dependencies:** none (gates #1 in production)

---

### Issue #5 — Exchange Key Verification

**Labels:** `phase-2` `backend` `P1` · **Estimate:** S

**Description**
`POST /api/exchange/keys/{id}/verify`: decrypt the stored key, call a cheap
authenticated Binance endpoint (account info), check spot-trade permission and
(ideally) that withdrawal permission is **disabled**, then set `verified=true`.

**Acceptance criteria**
- [ ] Endpoint returns granular result (invalid key / IP not allow-listed / missing trade permission / OK)
- [ ] Warns when the key has withdrawal permission enabled
- [ ] Dashboard/keys listing reflects verification state
- [ ] Executor (#1) refuses unverified keys

**Dependencies:** none

---

### Issue #6 — Alembic Migrations & Schema Fixes

**Labels:** `phase-3` `backend` `tech-debt` `P0` · **Estimate:** M

**Description**
Stand up Alembic (pinned, unused; `migrations/versions/` is empty) and replace
startup `create_all`. First migration bundles the known schema debt:
`recurring_purchase_id` FK → `ON DELETE SET NULL` (stop destroying transaction
history when a schedule is deleted), `DateTime(timezone=True)` columns, and an
index on `transactions (user_id, created_at)` for the paginated/timeline queries.

**Acceptance criteria**
- [ ] `alembic upgrade head` from empty DB reproduces current schema + fixes
- [ ] `create_all` removed from production startup path (kept for tests)
- [ ] Deleting a purchase preserves its transactions (regression test)
- [ ] Docs: migration workflow in README/docs

**Dependencies:** none (do before any Phase 2 schema additions)

---

### Issue #7 — Error Handling & Structured Logging

**Labels:** `phase-3` `backend` `P1` · **Estimate:** M

**Description**
Global exception handlers (uncaught → 500 with request id, never a stack trace
to the client), request-id middleware, and JSON logs via the already-pinned
`python-json-logger`, replacing the `print()` calls in `main.py`/`database.py`.
Log every trade-affecting action (key change, purchase CRUD, execution attempt)
with user id — this doubles as the audit trail.

**Acceptance criteria**
- [ ] All 5xx responses carry a request id that appears in the logs
- [ ] JSON log lines: timestamp, level, request id, user id, route, latency
- [ ] Secrets/keys provably never logged (test greps log output)
- [ ] `LOG_LEVEL` env respected

**Dependencies:** none

---

### Issue #8 — Input Validation Hardening & Rate Limiting

**Labels:** `phase-3` `backend` `security` `P1` · **Estimate:** M

**Description**
Add `slowapi` (Redis-backed — first real Redis consumer) with strict limits on
`/api/auth/*` (brute-force) and sane per-user limits elsewhere. Tighten
validation: supported-crypto allow-list for purchases, min/max `amount_usdt`,
withdrawal-address format check per chain, password strength beyond length 8.

**Acceptance criteria**
- [ ] Login limited (e.g. 5/min/IP) with 429 + Retry-After; register similarly
- [ ] Purchase creation rejects unsupported coins and out-of-range amounts
- [ ] Withdrawal address validated (per-chain regex/checksum) when provided
- [ ] Limits configurable via env; disabled in test runs

**Dependencies:** none

---

### Issue #9 — Comprehensive Test Suite for `api/`

**Labels:** `phase-3` `testing` `P0` · **Estimate:** L

**Description**
`tests/` currently covers only the legacy bot. Convert the existing 33-check
smoke script into pytest: unit tests (auth helpers, Fernet round-trip, masking,
frequency math), integration tests per router against a throwaway Postgres
(compose service or testcontainers), and an e2e happy path
(register → key → schedule → mocked execution → dashboard). Wire into CI.

**Acceptance criteria**
- [ ] `pytest tests/api` green locally and in CI (GitHub Actions with Postgres service)
- [ ] Every endpoint has success + auth-failure + isolation coverage
- [ ] Executor/scheduler tested with mocked Binance (no live calls in CI)
- [ ] Coverage gate ≥ 80 % on `api/`

**Dependencies:** #1, #2 for executor tests (router tests can start now)

---

### Issue #10 — Security Hardening

**Labels:** `phase-3` `security` `P0` · **Estimate:** M

**Description**
Close the known pre-launch gaps: refuse to boot in production with the default
`SECRET_KEY`; separate `ENCRYPTION_KEY` for Fernet (with a re-encrypt rotation
script) so JWT and at-rest crypto rotate independently; consume `ALLOWED_ORIGINS`
from env (CSV-tolerant parser) instead of the hard-coded list; logout +
refresh-token revocation (Redis denylist or token-version column); security
headers middleware; pin Postgres to localhost/private network in compose docs.

**Acceptance criteria**
- [ ] App exits with a clear error if `API_ENV=production` and default/weak secrets
- [ ] Fernet key independent of JWT secret; documented rotation procedure
- [ ] CORS origins fully env-driven; CSV value parses correctly
- [ ] `POST /api/auth/logout` invalidates the refresh token; test proves a revoked token fails
- [ ] Password change endpoint (revokes existing sessions)

**Dependencies:** none

---

### Issue #11 — API Documentation & Client Examples

**Labels:** `phase-3` `docs` `P2` · **Estimate:** S

**Description**
Polish the auto-generated OpenAPI: descriptions, request/response examples, and
error-shape docs on every route; tag descriptions; a `docs/API.md` quick-start
(register → login → schedule a purchase via curl) and a Postman/Bruno collection.

**Acceptance criteria**
- [ ] `/docs` shows examples and error responses for all 26+ endpoints
- [ ] curl walkthrough runs green top-to-bottom against a fresh dev stack
- [ ] Versioned OpenAPI JSON committed for client generation

**Dependencies:** stable Phase 2 surface

---

### Issue #12 — Monitoring, Health & Alerts

**Labels:** `phase-3` `ops` `P1` · **Estimate:** M

**Description**
Deep `/health` (DB, Redis, scheduler heartbeat, Binance reachability — the
legacy bot's selftest pattern is a good model), Prometheus metrics endpoint
(request rates/latency, purchases executed/failed, emails sent), Sentry (or
equivalent) for exceptions, and an alert when N consecutive executions fail or
the scheduler misses a heartbeat.

**Acceptance criteria**
- [ ] `/health` returns per-dependency status; orchestrator-friendly (200/503)
- [ ] `/metrics` scrapeable; dashboards for the four golden signals + business counters
- [ ] Failed-purchase alert fires in a simulated outage
- [ ] Runbook in `docs/`

**Dependencies:** #2 (scheduler heartbeat)

---

### Issue #13 — Customer Web UI

**Labels:** `phase-4` `frontend` `P1` · **Estimate:** XL

**Description**
Customer-facing SPA (React/Next.js; CORS already anticipates `localhost:3000`):
auth screens, KYC widget (Sumsub WebSDK, #4), API-key management, schedule
builder, transaction history, and the dashboard (holdings, upcoming buys,
timeline chart). The existing Flask `webapp/` is an internal VA tool for the
legacy bot — do not extend it; this is a separate product surface.

**Acceptance criteria**
- [ ] Full journey in-browser: register → KYC → add key → schedule → see dashboard
- [ ] Token refresh handled transparently; 401 → login redirect
- [ ] Responsive; deployable behind the same compose stack (or Vercel + API CORS)

**Dependencies:** #4 (KYC widget), stable Phase 2 API

---

### Issue #14 — Admin Dashboard

**Labels:** `phase-4` `backend` `frontend` `P2` · **Estimate:** L

**Description**
Role-gated admin surface (add `role` column via Alembic): user list with KYC
override + audit note, global purchase/failure feed, platform stats (volume,
active schedules, failure rate), and manual controls (disable user, force-retry
a failed purchase, pause the scheduler).

**Acceptance criteria**
- [ ] `role=admin` required (403 otherwise) on all admin routes; covered by tests
- [ ] KYC override writes an audit log entry (who/when/why)
- [ ] Failure feed links to the exact transaction + error

**Dependencies:** #6 (role migration), #7 (audit logging)

---

### Issue #15 — Advanced Analytics (P&L)

**Labels:** `phase-4` `backend` `analytics` `P2` · **Estimate:** M

**Description**
Extend the dashboard with live valuation: fetch current prices (public Binance
ticker, no keys needed), compute per-holding market value, unrealized P&L vs.
cost basis, and portfolio allocation; extend the timeline with cumulative
invested vs. current value.

**Acceptance criteria**
- [ ] `GET /api/dashboard/` holdings include `current_price`, `market_value`, `unrealized_pnl_usdt`, `unrealized_pnl_pct`
- [ ] Prices cached in Redis (~30 s TTL); price-source outage degrades gracefully to cost-basis-only
- [ ] Allocation + P&L covered by tests with fixed price fixtures

**Dependencies:** transactions flowing (#1)

---

### Issue #16 — Mobile App (optional / backlog)

**Labels:** `phase-4` `mobile` `P3` · **Estimate:** XL

**Description**
React Native/Expo client reusing the API: auth, dashboard, schedule management,
push notifications for executed/failed purchases (adds a device-token table +
FCM/APNs send in the notification service, #3). Revisit after web launch
validates demand.

**Acceptance criteria** (gate: post-web-launch)
- [ ] Feature parity with web for view + schedule management
- [ ] Push notification on purchase execution

**Dependencies:** #13 shipped and validated

---

## Suggested sequencing

| Sprint | Issues | Outcome |
|---|---|---|
| 1 | #6, #9 (routers), #10 | Safe foundation: migrations, tests, secrets |
| 2 | #1, #2, #5 | **Purchases actually execute** |
| 3 | #4, #3, #7 | Compliance + comms + observability of failures |
| 4 | #8, #12, #11 | Hardening + monitoring; API freeze |
| 5–7 | #13 | Web UI → **MVP launch** |
| post-launch | #14, #15, #16 | Admin, P&L, mobile |
