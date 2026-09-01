# AutoBuy Crypto Platform — Production Readiness Report

**Report generated:** 2026-09-01 · **Version:** 0.3.0 · **Target launch window:** Oct 26–30, 2026
**Status:** 🟡 **Staging-ready now — production launch gated on the prerequisites checklist below**

## Phase completion status

### Phase 1: MVP Foundation ✅ COMPLETE (Sprint 0)
- [x] Async FastAPI + PostgreSQL 16 + Redis 7
- [x] JWT auth (Argon2, access/refresh separation)
- [x] Full API surface (auth, user, kyc, exchange, purchases, transactions, dashboard, analytics — 35+ endpoints)
- [x] Alembic migrations (5 revisions) with cascade-safety fix — deleting a schedule preserves its history

### Phase 2: Automation ✅ COMPLETE (Sprints 1–2)
- [x] Purchase execution on Binance (reusing the battle-tested legacy client; due-date idempotency)
- [x] APScheduler recurring automation (advisory-lock replica safety, no catch-up sprees, 1h failure retry)
- [x] Email notifications (Resend, fire-and-forget) + KYC via Sumsub (self-serve verification hole closed)
- [x] Exchange key verification gating the executor
- [x] Validation hardening (amount bounds, crypto allow-list, per-chain addresses, password policy)
- [x] Security audit: boot guard, ENCRYPTION_KEY split + rotation script, logout/token revocation, bandit clean
- [x] Rate limiting + login lockout (Redis) · performance pass (indexes, pooling, middleware consolidation)

### Phase 3: Production Deployment ✅ COMPLETE (Sprint 3)
- [x] Prometheus /metrics + alert thresholds + runbook (docs/ALERTING.md); CRITICAL alert on 5 consecutive execution failures (tested)
- [x] Dockerfile.api: migrations-on-boot → gunicorn ×4 uvicorn workers; compose prod/monitoring profiles; nginx config
- [x] OpenAPI polish with documentation enforced by tests; docs/API.md, DEPLOYMENT.md, EXAMPLES.md
- [x] React frontend scaffold (typed client, JWT lifecycle, auth-guarded routing)

### Phase 4: Full-Stack Release ✅ COMPLETE (Sprint 4)
- [x] Web dashboard: stats, holdings + allocation pie, investment timeline, upcoming buys, recent transactions
- [x] Purchase management UI: create/edit/pause/delete with mirrored validation
- [x] Mobile scaffold (Expo: navigation, offline-caching API client) — *full app is Phase 5*
- [x] Email campaigns: welcome, first purchase, weekly digest, milestones (Jinja2, 7 templates × HTML+text)
- [x] Advanced analytics: live P&L, diversification, spend projections, volatility-based DCA guidance

## Test & quality metrics

| Metric | Value |
|---|---|
| API tests | **152 passed** (+2 perf tests run per-process) |
| Frontend tests | 9 passed · TypeScript strict, clean |
| Coverage | **93%** on `api/` (CI gate at 85%, GitHub Actions with Postgres+Redis services) |
| Security | bandit clean at high severity; secrets provably absent from logs |
| Load (4 workers) | p50 18ms / **p95 245ms** @ 10 concurrent; 1,000-purchase scheduler backlog in 32s, zero duplicates |

## Prerequisites for production (the launch gate)

1. [ ] **Sumsub sandbox→prod credentials** (`SUMSUB_APP_TOKEN`, `SUMSUB_SECRET_KEY`, `SUMSUB_WEBHOOK_SECRET`) — integration is mock-tested only; embed the WebSDK widget once credentials exist
2. [ ] **Resend API key** + verified sending domain
3. [ ] **Live-trade verification on Binance testnet**, then a small-amount canary with `AUTOBUY_DRY_RUN=0` (today every order is simulated)
4. [ ] TLS certificate + DNS for `api.autobuy.app`; deploy `deployment/nginx.conf`
5. [ ] Strong `SECRET_KEY` + `ENCRYPTION_KEY` in production env (boot guard enforces this)
6. [ ] Automated database backups on a schedule + one tested restore
7. [ ] Prometheus scraping `/metrics` with the docs/ALERTING.md rules wired to a pager
8. [ ] Remove the temporary `/test-purchase` routes (tracked since Sprint 1)

## Deployment steps

```bash
docker build -f Dockerfile.api -t autobuy-api:v0.3.0 .
docker tag autobuy-api:v0.3.0 <registry>/autobuy-api:v0.3.0 && docker push <registry>/autobuy-api:v0.3.0
docker compose --profile prod up -d        # entrypoint applies migrations, then serves
curl https://api.autobuy.app/health        # scheduler state included
```

## Launch timeline

- **Weeks 1–2 (staging):** deploy, wire Sumsub/Resend sandbox, testnet trading run, load test, security re-check
- **Weeks 3–4 (limited launch):** ~100 users, `AUTOBUY_DRY_RUN=0` behind small per-user caps, KYC + campaigns live
- **Weeks 5–6:** open signup; begin Phase 5 mobile implementation

## Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Live-order defect (double buy / wrong size) | Critical | Idempotent per due-date (tested), dry-run default, testnet first, canary with small caps |
| Binance API change / ban | High | Pinned legacy client, error-budget alert (>10/h), granular key re-verification |
| Database loss | Critical | Automated backups + tested restore (prerequisite #6); migrations reproducible from empty DB |
| Load surge / DDoS | High | slowapi + nginx rate limits, 4-worker scaling, horizontal replicas (advisory-lock safe) |
| KYC false rejections | Medium | Status sync endpoint, support escalation path in rejection email |
| Email provider outage | Low | Fire-and-forget by contract — trades never blocked; failure-ratio alert |

## Phase 5 (post-launch)

Mobile app implementation (screens, push) · rebalancing automation on the analytics foundation ·
admin dashboard (GH #18) · multi-exchange execution (legacy Bitget/Bybit/OKX clients ready, GH #21) ·
outbound webhooks (GH #22) · client SDKs from the committed OpenAPI spec (GH #23) · CSV/tax reporting (GH #24)

## Support

- Support email: support@minastech.ca
- Issues & board: https://github.com/erwingarcia1/Minas-AI-Asset-Buy---Bitget---Private/issues · https://github.com/users/johngreat-minas/projects/1
- Runbook: docs/ALERTING.md · Ops guide: docs/DEPLOYMENT.md
