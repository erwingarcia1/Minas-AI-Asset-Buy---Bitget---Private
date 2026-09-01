# AutoBuy Crypto Platform — GitHub Issues

*Generated 2026-09-01 from a full codebase scan. Source analysis: [`docs/PRODUCT_BACKLOG.md`](docs/PRODUCT_BACKLOG.md).*
*Story points: Fibonacci scale (1, 2, 3, 5, 8, 13, 21). Priorities: CRITICAL (blocks other work) → HIGH (MVP launch) → MEDIUM (nice for launch) → LOW (post-launch).*

---

## Summary

### Issues by type

| Type | Count | Story Points |
|---|---:|---:|
| Feature | 14 | 114 |
| Tech Debt | 2 | 13 |
| Testing | 4 | 24 |
| Task (ops/docs) | 4 | 16 |
| **Total** | **24** | **167** |

### Issues by phase and priority

| Phase | CRITICAL | HIGH | MEDIUM | LOW | Points | Timeline |
|---|---:|---:|---:|---:|---:|---|
| 2 — Core Services | 3 | 3 | — | — | 42 | 2–3 weeks |
| 3 — Testing & Stability | 3 | 1 | 1 | — | 32 | 1–2 weeks |
| 4 — Production Readiness | 1 | 3 | 1 | — | 21 | 1 week |
| 5 — Frontend | 1 | 1 | — | 1 | 50 | 3–4 weeks (parallel after Phase 2) |
| 6 — Nice-to-Have | — | — | 1 | 4 | 22 | post-launch |
| **Total** | **8** | **8** | **3** | **5** | **167** | **~7–8 weeks to MVP** |

### Team effort breakdown (see ROADMAP.md for capacity)

| Track | Issues | Points | Owner profile |
|---|---:|---:|---|
| Backend services & integrations | #1–#6, #12, #14, #20–#23 | 69 | Backend/Python |
| Testing & security | #7–#11 | 32 | Backend + reviewer |
| Ops / deploy / docs | #13, #15, #16 | 13 | Backend/DevOps |
| Frontend | #17–#19 | 50 | Frontend |
| Reporting | #24 | 3 | Backend |

**Critical path:** #12 (Alembic) → #1 (Executor) → #2 (Scheduler) → #8/#7 (tests) → #9 (security audit) → #15 (deploy) → #17 (web UI) → launch.
⚠️ #12 lives in Phase 4 per the roadmap phases, but it **blocks any schema change** — schedule it first (see ROADMAP.md, Sprint 0).

---

# PHASE 1: COMPLETED ✅ — Infrastructure & API Routes

**26 endpoints live** (24 business routes + 2 service routes; FastAPI additionally auto-serves `/docs` and `/openapi.json`). All verified by a 33-check end-to-end smoke run against live dev Postgres on 2026-08-31.

| # | Method | Path | Status |
|---:|---|---|---|
| 1 | POST | `/api/auth/register` | ✅ 400 on duplicate email, Argon2 hash |
| 2 | POST | `/api/auth/login` | ✅ access + refresh JWT pair |
| 3 | POST | `/api/auth/verify` | ✅ token validity + user id |
| 4 | POST | `/api/auth/refresh` | ✅ rejects access-as-refresh, checks user exists |
| 5 | GET | `/api/user/profile` | ✅ authenticated self-profile |
| 6 | PUT | `/api/user/profile` | ✅ email uniqueness, KYC allow-list (⚠️ self-serve until #3) |
| 7 | GET | `/api/user/{user_id}` | ✅ public profile, 404 handling |
| 8 | POST | `/api/exchange/keys` | ✅ Fernet-encrypted upsert |
| 9 | GET | `/api/exchange/keys` | ✅ masked display (`AKIA...2345`) |
| 10 | DELETE | `/api/exchange/keys/{key_id}` | ✅ owner-scoped, 404 |
| 11 | POST | `/api/purchases/` | ✅ frequency validation, next-buy tomorrow |
| 12 | GET | `/api/purchases/` | ✅ user-scoped list |
| 13 | GET | `/api/purchases/{id}` | ✅ |
| 14 | PUT | `/api/purchases/{id}` | ✅ partial update |
| 15 | DELETE | `/api/purchases/{id}` | ✅ (⚠️ cascade-deletes history until #12) |
| 16 | PATCH | `/api/purchases/{id}/toggle` | ✅ |
| 17 | GET | `/api/transactions/` | ✅ pagination + status filter |
| 18 | GET | `/api/transactions/{id}` | ✅ |
| 19 | GET | `/api/transactions/stats/summary` | ✅ single-query aggregates |
| 20 | GET | `/api/transactions/crypto/{crypto}` | ✅ |
| 21 | GET | `/api/transactions/status/{tx_status}` | ✅ 400 on bad status |
| 22 | GET | `/api/dashboard/` | ✅ holdings, active + upcoming, monthly avg |
| 23 | GET | `/api/dashboard/stats` | ✅ |
| 24 | GET | `/api/dashboard/analytics/timeline` | ✅ zero-filled daily buckets |
| 25 | GET | `/health` | ✅ shallow (deepened by #13) |
| 26 | GET | `/` | ✅ welcome/docs pointer |

**What's working:** JWT auth with refresh/access separation · per-user data isolation (proven with two-user test) · Fernet encryption at rest · timezone-aware UTC responses · Postgres 16 + Redis 7 compose stack with healthchecks · async SQLAlchemy 2 throughout.

**What Phase 1 does NOT do:** execute purchases, run schedules, send email, verify KYC, verify exchange keys, or migrate schemas. That is Phases 2–4 below.

---

# PHASE 2: CORE SERVICES (next 2–3 weeks)

## [CRITICAL] Build Purchase Execution Service

**Type:** Feature

**Labels:** phase-2, backend, purchase-execution, binance, critical

**Story Points:** 13

**Dependencies:**
- Phase 1 API endpoints complete ✅
- Binance API key storage (exchange.py) ✅
- #12 Alembic baseline strongly recommended first (schema will gain columns)

**Description:**
Build the service that executes actual cryptocurrency purchases on Binance when scheduled purchases are triggered. This is the core business logic that converts scheduled purchases into real transactions. Decrypt the user's stored API key, place a quote-denominated spot market buy for `amount_usdt` of `crypto`, and record a `Transaction` row for every attempt.

**Acceptance Criteria:**
- [ ] Can connect to Binance API using stored (decrypted) credentials
- [ ] Validates purchase request (amount, crypto, withdrawal address if set)
- [ ] Executes order on Binance and stores `binance_order_id`
- [ ] Records transaction in database with status for every attempt, success or failure
- [ ] Handles API errors gracefully with retry logic (exponential backoff, capped)
- [ ] Idempotent per due-date — a retry never double-buys
- [ ] Skips users with `kyc_status != verified`, disabled schedules, and unverified keys
- [ ] Failure taxonomy mapped to `error_message`: insufficient balance, invalid key, min-notional, symbol not found
- [ ] Dry-run/testnet mode via config flag
- [ ] Logs all purchase attempts for audit trail

**Technical Notes:**
- Create `api/services/purchase_service.py`
- **Reuse the legacy `binance_client.py`** — it already implements normalized quote-denominated market buys, symbol precision flooring, and min-notional handling; do not rewrite against raw `python-binance`
- Store `binance_order_id` in the transaction record
- Idempotency: unique constraint or executed-date marker per (purchase, due date)

**Testing Requirements:**
- Mock Binance API calls (no live calls in CI)
- Test success scenario
- Test network failure handling and backoff
- Test invalid credentials
- Test insufficient funds scenario
- Test double-execution guard

---

## [CRITICAL] Recurring Purchase Scheduler (APScheduler)

**Type:** Feature

**Labels:** phase-2, backend, scheduler, critical

**Story Points:** 8

**Dependencies:**
- #1 Purchase Execution Service

**Description:**
`AsyncIOScheduler` started in the FastAPI lifespan. Polls for `enabled` purchases with `next_purchase_date <= now`, invokes the executor, then advances `next_purchase_date` by frequency (daily +1d, weekly +7d, biweekly +14d, monthly +1 calendar month).

**Acceptance Criteria:**
- [ ] Due purchases execute within one poll interval (configurable, default 60 s)
- [ ] `next_purchase_date` advances even when execution fails (no hot-loop retry storms); capped retry policy
- [ ] Single-instance safety: Redis or Postgres advisory lock so two API replicas never double-execute
- [ ] Scheduler heartbeat exposed on `/health`
- [ ] Graceful shutdown — in-flight job completes before the app exits

**Technical Notes:**
- Create `api/services/scheduler.py`; `apscheduler==3.10.4` already pinned
- Redis is already provisioned in compose and unused — natural home for the lock

**Testing Requirements:**
- Date advancement for all four frequencies, incl. month-end edges (Jan 31 → Feb 28)
- Lock contention test (two schedulers, one execution)
- Failure path: executor raises → date still advances, retry capped

---

## [CRITICAL] KYC Verification Flow (Sumsub)

**Type:** Feature

**Labels:** phase-2, backend, compliance, sumsub, critical

**Story Points:** 8

**Dependencies:**
- None (gates #1 in production)

**Description:**
Replace the manual `kyc_status` field with a real flow. `POST /api/kyc/token` creates a Sumsub applicant (persist `kyc_sumsub_id`) and returns a WebSDK access token; `POST /api/kyc/webhook` receives HMAC-verified review results and updates `kyc_status`. **Remove `kyc_status` from user-writable profile fields** — the current self-serve toggle is a compliance hole the moment real money moves.

**Acceptance Criteria:**
- [ ] Applicant created lazily on first token request; id persisted
- [ ] Webhook signature verified; GREEN → verified, RED (final) → rejected
- [ ] `kyc_status` no longer writable via `PUT /api/user/profile` (route + tests updated)
- [ ] Purchase execution gated on `verified`
- [ ] Sandbox integration test with mocked webhook payloads

**Technical Notes:**
- New router `api/routes/kyc.py` (prefix `/api/kyc` — already anticipated in main.py's original TODO)
- `SUMSUB_APP_TOKEN` / `SUMSUB_API_URL` already in config; call REST API via `httpx` (SDK is commented out in requirements)

**Testing Requirements:**
- Webhook with bad signature → 401, status unchanged
- GREEN/RED payloads → correct status transitions
- Regression: profile update can no longer set kyc_status

---

## [HIGH] Email Notification Service (Resend)

**Type:** Feature

**Labels:** phase-2, backend, notifications, resend, high

**Story Points:** 5

**Dependencies:**
- #1 (receipt/failure events)

**Description:**
`api/services/email_service.py` with templated transactional email via Resend. Events: welcome on register, purchase executed (receipt), purchase failed (reason + fix hint), KYC status change.

**Acceptance Criteria:**
- [ ] Fire-and-forget background sends — an email failure never fails the triggering request or trade
- [ ] All sends logged; clean no-op when `RESEND_API_KEY` unset (dev)
- [ ] Plain-text + HTML templates for the four events
- [ ] Notification-preference field on `users` honored for non-critical mail

**Technical Notes:**
- `RESEND_API_KEY` / `SUPPORT_EMAIL` already in config; un-comment `resend` in requirements or call REST via httpx
- Send from FastAPI `BackgroundTasks` (or the scheduler loop for trade events)

**Testing Requirements:**
- Mocked Resend client; assert payloads per event
- Email failure does not propagate to caller
- Preference opt-out respected

---

## [HIGH] Exchange API Key Verification

**Type:** Feature

**Labels:** phase-2, backend, binance, high

**Story Points:** 3

**Dependencies:**
- None

**Description:**
`POST /api/exchange/keys/{key_id}/verify`: decrypt the stored key, call a cheap authenticated Binance endpoint (account info), confirm spot-trade permission, set `verified=true`. Warn when the key has withdrawal permission enabled (it shouldn't).

**Acceptance Criteria:**
- [ ] Granular result: invalid key / IP not allow-listed / missing trade permission / OK
- [ ] Withdrawal-permission warning surfaced in the response
- [ ] Key listing reflects verification state
- [ ] Executor (#1) refuses unverified keys

**Testing Requirements:**
- Mocked Binance account endpoint for each result class

---

## [HIGH] Input Validation & Error Handling

**Type:** Feature

**Labels:** phase-2, backend, validation, high

**Story Points:** 5

**Dependencies:**
- None

**Description:**
Global exception handlers (uncaught → 500 with request id, never a stack trace to the client), request-id middleware, and tighter domain validation: supported-crypto allow-list for purchases, min/max `amount_usdt`, per-chain withdrawal-address format checks, password policy beyond min-length-8.

**Acceptance Criteria:**
- [ ] All 5xx responses carry a request id that appears in the logs
- [ ] Purchase creation rejects unsupported coins and out-of-range amounts (400 with clear message)
- [ ] Withdrawal address validated per chain when provided
- [ ] Consistent error envelope `{detail, request_id}` across all routes

**Technical Notes:**
- Supported-coin list in config, not hard-coded in the route
- Pairs with #13 (structured logging) — build the middleware once

**Testing Requirements:**
- Table-driven validation tests per field
- Forced unhandled exception → 500 envelope without traceback

---

# PHASE 3: TESTING & STABILITY (1–2 weeks)

## [CRITICAL] Unit Tests for All Services

**Type:** Testing

**Labels:** phase-3, testing, critical

**Story Points:** 8

**Dependencies:**
- #1, #2 (services must exist)

**Description:**
Pytest unit coverage for the service layer: executor (mocked exchange), scheduler date math, email templating, KYC webhook parsing, plus the existing helpers (JWT create/verify, Fernet round-trip, `mask_api_key`, frequency validation).

**Acceptance Criteria:**
- [ ] `pytest tests/api/unit` green locally and in CI, no network access
- [ ] Executor failure taxonomy fully covered
- [ ] Scheduler: all four frequencies + month-end edges
- [ ] Coverage ≥ 80 % on `api/services/`

---

## [CRITICAL] Integration Tests for API Routes

**Type:** Testing

**Labels:** phase-3, testing, critical

**Story Points:** 8

**Dependencies:**
- Phase 1 ✅ (can start immediately)

**Description:**
Convert the existing 33-check smoke script into a proper pytest suite against a throwaway Postgres (compose service or testcontainers): every endpoint gets success, auth-failure, and cross-user isolation cases. Wire into GitHub Actions with a Postgres service container.

**Acceptance Criteria:**
- [ ] Every endpoint: 2xx path + 401 without token + 404/isolation with another user's ids
- [ ] Runs in CI on every PR; DB state isolated per test session
- [ ] Existing smoke assertions all preserved

---

## [CRITICAL] Security Audit & Hardening

**Type:** Tech Debt

**Labels:** phase-3, security, critical

**Story Points:** 8

**Dependencies:**
- None (findings feed #15 deploy config)

**Description:**
Close the known pre-launch gaps and sweep for new ones: refuse to boot in production with the default `SECRET_KEY`; separate `ENCRYPTION_KEY` for Fernet (with re-encrypt rotation script) so JWT and at-rest crypto rotate independently; consume `ALLOWED_ORIGINS` from env (CSV-tolerant — the var is documented in `.env.example` but never read today); logout + refresh-token revocation; security-headers middleware; dependency vulnerability scan.

**Acceptance Criteria:**
- [ ] App exits with a clear error if `API_ENV=production` and default/weak secrets
- [ ] Fernet key independent of JWT secret; rotation procedure documented and scripted
- [ ] CORS origins fully env-driven; CSV value parses correctly
- [ ] `POST /api/auth/logout` invalidates the refresh token (test proves a revoked token fails)
- [ ] Password-change endpoint that revokes existing sessions
- [ ] `pip-audit`/`safety` clean or waivered in CI

---

## [HIGH] End-to-End Test Suite

**Type:** Testing

**Labels:** phase-3, testing, e2e, high

**Story Points:** 5

**Dependencies:**
- #1, #2, #3 (full journey must exist)

**Description:**
One scripted journey against a full stack: register → (mock) KYC verified → add + verify key → create schedule → scheduler fires with mocked Binance → transaction recorded → dashboard reflects it → email event emitted.

**Acceptance Criteria:**
- [ ] Journey runs green against `docker compose up` stack in CI (nightly is acceptable)
- [ ] Asserts DB state, API responses, and emitted email payloads at each step

---

## [MEDIUM] Performance & Load Testing

**Type:** Testing

**Labels:** phase-3, testing, performance, medium

**Story Points:** 3

**Dependencies:**
- #8 (integration harness)

**Description:**
Locust or k6 baseline: dashboard and transaction-list endpoints under realistic load; scheduler tick with 1 000 due purchases; identify N+1s and missing indexes (the `transactions (user_id, created_at)` index lands in #12).

**Acceptance Criteria:**
- [ ] p95 < 300 ms for dashboard/list endpoints at 50 concurrent users on dev hardware
- [ ] Scheduler processes 1 000 due purchases without missed/duplicated executions
- [ ] Findings filed as issues with flame-graph/EXPLAIN evidence

---

# PHASE 4: PRODUCTION READINESS (1 week)

## [CRITICAL] Database Migrations (Alembic) & Schema Fixes

**Type:** Tech Debt

**Labels:** phase-4, backend, database, critical

**Story Points:** 5

**Dependencies:**
- None — ⚠️ **schedule first (Sprint 0)**: every Phase 2 issue that adds a column depends on a migration baseline

**Description:**
Stand up Alembic (pinned but unused; `migrations/versions/` is empty) and replace startup `create_all`. First migration bundles known schema debt: `transactions.recurring_purchase_id` FK → `ON DELETE SET NULL` (today, deleting a schedule destroys its transaction history via ORM cascade), `DateTime(timezone=True)` columns, index on `transactions (user_id, created_at)`.

**Acceptance Criteria:**
- [ ] `alembic upgrade head` from an empty DB reproduces current schema + fixes
- [ ] `create_all` removed from the production startup path (kept for tests)
- [ ] Deleting a purchase preserves its transactions (regression test)
- [ ] Migration workflow documented in README/docs

---

## [HIGH] Monitoring & Structured Logging

**Type:** Task

**Labels:** phase-4, ops, observability, high

**Story Points:** 5

**Dependencies:**
- #2 (scheduler heartbeat), #6 (request-id middleware)

**Description:**
JSON logs via the pinned `python-json-logger` (timestamp, level, request id, user id, route, latency), deep `/health` (DB, Redis, scheduler heartbeat, Binance reachability — the legacy bot's `--selftest` is a good model), Prometheus `/metrics`, Sentry for exceptions, alert on consecutive execution failures or missed scheduler heartbeat.

**Acceptance Criteria:**
- [ ] `/health` per-dependency status, 200/503 orchestrator-friendly
- [ ] `/metrics` scrapeable: golden signals + purchases executed/failed, emails sent
- [ ] Secrets provably never logged (test greps output)
- [ ] Failed-purchase alert fires in a simulated outage; runbook in `docs/`

---

## [HIGH] Rate Limiting

**Type:** Feature

**Labels:** phase-4, security, high

**Story Points:** 3

**Dependencies:**
- Redis (provisioned ✅)

**Description:**
Redis-backed `slowapi`: strict limits on `/api/auth/*` (brute-force protection), sane per-user limits elsewhere.

**Acceptance Criteria:**
- [ ] Login limited (e.g. 5/min/IP) with 429 + `Retry-After`; register similarly
- [ ] Per-user default limit on authenticated routes
- [ ] Limits env-configurable; disabled in test runs

---

## [HIGH] Deployment Configuration

**Type:** Task

**Labels:** phase-4, ops, deploy, high

**Story Points:** 5

**Dependencies:**
- #9 (security findings), #12 (migrations run on deploy)

**Description:**
Production deploy path for the API: add the API service to docker-compose (currently compose only runs Postgres/Redis; the API runs from a terminal), multi-stage Dockerfile, `alembic upgrade` on release, env templating, TLS/reverse-proxy guidance, and CI/CD (build → test → deploy). Reuse patterns from the existing `deploy/` docs (EC2/Alibaba, systemd) where they fit.

**Acceptance Criteria:**
- [ ] `docker compose --profile prod up` brings up API + Postgres + Redis with healthchecks
- [ ] Release runs migrations before serving traffic
- [ ] CI pipeline: lint → unit → integration → build image
- [ ] Deployment runbook in `deploy/`

---

## [MEDIUM] API Documentation & Client Examples

**Type:** Task

**Labels:** phase-4, docs, medium

**Story Points:** 3

**Dependencies:**
- Stable Phase 2 surface

**Description:**
Polish auto-generated OpenAPI: descriptions, request/response examples, and error shapes on every route; tag descriptions; `docs/API.md` curl quick-start (register → login → schedule); Postman/Bruno collection; committed versioned OpenAPI JSON for client generation.

**Acceptance Criteria:**
- [ ] `/docs` shows examples + error responses for all endpoints
- [ ] curl walkthrough runs green top-to-bottom on a fresh dev stack
- [ ] OpenAPI JSON committed and versioned

---

# PHASE 5: FRONTEND (3–4 weeks — can start in parallel after Phase 2)

## [CRITICAL] Customer Web Dashboard (React)

**Type:** Feature

**Labels:** phase-5, frontend, critical

**Story Points:** 21

**Dependencies:**
- Phase 2 API stable, #3 (Sumsub WebSDK token endpoint)

**Description:**
Customer-facing SPA (React/Next.js; CORS already anticipates `localhost:3000`): auth screens, Sumsub KYC widget, API-key management, schedule builder, transaction history, dashboard (holdings, upcoming buys, timeline chart). Note: the existing Flask `webapp/` is an internal VA tool for the legacy bot — this is a separate product surface; do not extend it.

**Acceptance Criteria:**
- [ ] Full journey in-browser: register → KYC → add key → schedule → dashboard
- [ ] Transparent token refresh; 401 → login redirect
- [ ] Responsive; deployable behind the compose stack or Vercel + API CORS
- [ ] Component tests for auth flow and schedule builder; one Playwright happy path

---

## [HIGH] Admin Panel

**Type:** Feature

**Labels:** phase-5, frontend, backend, admin, high

**Story Points:** 8

**Dependencies:**
- #12 (role column migration), #13 (audit logging)

**Description:**
Role-gated admin surface (`role` column via Alembic): user list with KYC override + audit note, global purchase/failure feed, platform stats (volume, active schedules, failure rate), manual controls (disable user, force-retry a failed purchase, pause scheduler).

**Acceptance Criteria:**
- [ ] `role=admin` required (403 otherwise) on all admin routes; covered by tests
- [ ] KYC override writes an audit entry (who/when/why)
- [ ] Failure feed links to the exact transaction + error

---

## [LOW] Mobile App (React Native)

**Type:** Feature

**Labels:** phase-5, mobile, low, backlog

**Story Points:** 21

**Dependencies:**
- #17 shipped and validated (post-launch gate)

**Description:**
React Native/Expo client reusing the API: auth, dashboard, schedule management, push notifications for executed/failed purchases (device-token table + FCM/APNs in the notification service). Revisit after web launch validates demand.

**Acceptance Criteria:**
- [ ] Feature parity with web for viewing + schedule management
- [ ] Push notification on purchase execution

---

# PHASE 6: NICE-TO-HAVE (future)

## [MEDIUM] Advanced Analytics (P&L & Allocation)

**Type:** Feature

**Labels:** phase-6, analytics, medium

**Story Points:** 5

**Dependencies:**
- Transactions flowing (#1)

**Description:**
Live valuation on the dashboard: public Binance ticker prices (no keys needed), per-holding market value, unrealized P&L vs cost basis, portfolio allocation; timeline extended with cumulative invested vs current value.

**Acceptance Criteria:**
- [ ] Holdings include `current_price`, `market_value`, `unrealized_pnl_usdt`, `unrealized_pnl_pct`
- [ ] Prices cached in Redis (~30 s TTL); price-source outage degrades to cost-basis-only
- [ ] Tested with fixed price fixtures

---

## [LOW] Multi-Exchange Support

**Type:** Feature

**Labels:** phase-6, backend, exchanges, low

**Story Points:** 8

**Dependencies:**
- #1 (executor abstraction)

**Description:**
Extend key storage and execution beyond Binance. **Major head start:** the legacy codebase already ships normalized clients for Bitget, Bybit, and OKX (`bitget_client.py`, `bybit_client.py`, `okx_client.py`) with an identical interface to `binance_client.py` — the work is plumbing them through the platform's key storage, verification, and executor, not writing exchange integrations.

**Acceptance Criteria:**
- [ ] `exchange` field accepts bitget/bybit/okx; per-exchange key verification
- [ ] Executor routes to the right client by key's exchange
- [ ] Per-venue symbol formats handled (OKX `BTC-USDT` vs `BTCUSDT`)

---

## [LOW] Webhook Notifications

**Type:** Feature

**Labels:** phase-6, backend, integrations, low

**Story Points:** 3

**Description:**
User-configurable outbound webhooks (purchase executed/failed): URL + HMAC signing secret per user, delivery with retries and a dead-letter log.

**Acceptance Criteria:**
- [ ] CRUD for webhook endpoints; test-ping button
- [ ] Signed payloads (HMAC header), 3 retries with backoff, failures visible to the user

---

## [LOW] API Client SDKs

**Type:** Task

**Labels:** phase-6, docs, sdk, low

**Story Points:** 3

**Dependencies:**
- #16 (committed OpenAPI spec)

**Description:**
Generate and publish TypeScript + Python clients from the versioned OpenAPI spec; smoke-test both against staging in CI.

**Acceptance Criteria:**
- [ ] `openapi-generator` (or similar) pipeline producing both clients
- [ ] Each client's README shows the register→schedule flow

---

## [LOW] Advanced Reporting

**Type:** Feature

**Labels:** phase-6, analytics, reporting, low

**Story Points:** 3

**Description:**
CSV/PDF exports of transaction history and cost-basis summaries (tax-season friendly): date-range filters, per-crypto grouping, average-cost method noted.

**Acceptance Criteria:**
- [ ] `GET /api/transactions/export?format=csv&from=&to=` streams a well-formed CSV
- [ ] Cost-basis summary matches `/stats/summary` figures exactly

---

*End of issues. See [`ROADMAP.md`](ROADMAP.md) for the timeline, milestones, capacity, and risk assessment.*
