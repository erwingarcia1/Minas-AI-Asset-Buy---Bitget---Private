# Deployment & Operations Guide

## Docker

```bash
docker build -f Dockerfile.api -t autobuy-api:latest .   # platform image
docker compose --profile prod up -d --build              # API + Postgres + Redis
docker compose --profile monitoring up -d                # + Prometheus (:9090)
```

The compose project is pinned to `autobuy-platform` (top-level `name:` in
docker-compose.yml), so these commands are safe to run on the production box
where the legacy bot's project (`autobuy`, docker-compose.legacy.yml) is also
running — the two stacks can never displace each other's containers. Never
run `--remove-orphans` against the legacy project. On the server, deploys go
through `scripts/deploy.sh platform` (see the README's AWS section).

The entrypoint applies Alembic migrations, then serves via **gunicorn with 4
uvicorn workers** (measured: p95 245ms @10 concurrent vs 1.6s single-worker).
The root `Dockerfile` belongs to the legacy sub-account bot — don't confuse them.

## Production deployment checklist

Work through this top to bottom before the first `scripts/deploy.sh platform`
on a production host. Items marked done are already in the repository; the
rest are manual and tracked as Phase 7 issues.

| # | Item | Status | Where |
|---|---|---|---|
| 1 | Credential files ignored by git (`.env`, every `.env.*`, `.envy`, `*.pem`) | ✅ done | `.gitignore` |
| 2 | Dev-only `/test-purchase` routes removed | ✅ done (#34) | `api/main.py` |
| 3 | Production env template with every required variable | ✅ done | `.env.production.example` |
| 4 | TLS + DNS: real hostname (`api.autobuy.app`) and certificate paths in `deployment/nginx.conf`; DNS A/AAAA records; HTTP→HTTPS redirect verified | ⬜ manual (#31) | `deployment/nginx.conf` |
| 5 | Credentials provisioned into the server's root-owned `.env` (`chmod 600`): `SECRET_KEY`, `ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, Resend (#28), Sumsub (#27); optional Twilio (#29), FCM (#30), Slack | ⬜ manual | `.env.production.example` |
| 6 | `.env` validated (command below) | ⬜ manual | `scripts/validate_env.py` |
| 7 | Sumsub webhook pointed at `https://<api host>/api/webhooks/sumsub` with the same secret as `SUMSUB_WEBHOOK_SECRET`; Resend sending domain verified (SPF/DKIM) | ⬜ manual (#27, #28) | provider dashboards |
| 8 | Daily `pg_dump` scheduled to off-host storage, retention set, one restore rehearsed | ⬜ manual (#32) | "Database" section |
| 9 | Prometheus scraping `/metrics`; `docs/ALERTING.md` rules routed to a pager with an on-call rotation | ⬜ manual (#33) | `deployment/prometheus.yml` |
| 10 | Binance testnet run, then a small-amount canary before `AUTOBUY_DRY_RUN=0` | ⬜ manual | `PRODUCTION_READINESS.md` |
| 11 | `main` is the default branch and the deploy key pulls it | ⬜ manual | GitHub settings |

### Validate `.env` before every deploy

```bash
python scripts/validate_env.py .env          # on the server; default path is ./.env
```

It exits non-zero and lists one line per problem when a required variable is
missing, still a placeholder, or too weak for the API's boot guard (defaults,
keys under 32 characters, `SECRET_KEY` equal to `ENCRYPTION_KEY`, the compose
default Postgres password). Warnings cover disabled features, plain-http
origins, and `AUTOBUY_DRY_RUN=0`. Values are never printed. `scripts/deploy.sh
platform` performs its own, narrower check afterwards.

## Web frontend

The SPA in `frontend/` is static files; it is not part of the API image.
Build it on the server (or in CI) and let nginx serve it:

```bash
cd frontend && npm ci && npm run build          # -> frontend/dist
sudo rsync -a --delete frontend/dist/ /var/www/autobuy/frontend/dist/
sudo nginx -t && sudo systemctl reload nginx
```

`deployment/nginx.conf` has two vhosts: `api.example.com` (API only) and
`app.example.com`, which serves `frontend/dist` with an SPA fallback, forwards
`/api/` and `/health` to the API upstream, and sets a Content-Security-Policy
that allows only Sumsub's origins (`api.sumsub.com`, `in.sumsub.com`,
`static.sumsub.com`) for the KYC widget. Replace the hostnames and certificate
paths. Because the app calls `/api` on its own origin (`VITE_API_BASE_URL=/api`),
no CORS entry is needed for it; set `APP_BASE_URL=https://app.example.com` so
emailed links open the app. GitHub Actions (`frontend-tests.yml`) installs,
builds and tests the app on every change under `frontend/`.

## Environment variables

| Variable | Required in prod | Notes |
|---|---|---|
| `SECRET_KEY` | ✔ (≥32 chars) | JWT signing; boot refuses defaults |
| `ENCRYPTION_KEY` | ✔ | At-rest Fernet for exchange keys; rotate with `scripts/rotate_encryption_key.py` |
| `DATABASE_URL` | ✔ | `postgresql+asyncpg://…` |
| `REDIS_URL` | ✔ | rate limits, lockout, token revocation, scheduler lock |
| `AUTOBUY_DRY_RUN` | default `1` | **`0` enables real orders** |
| `SCHEDULER_ENABLED` | default `1` | emergency stop switch |
| `ALLOWED_ORIGINS` | recommended | CSV or JSON array |
| `RESEND_API_KEY` | ✔ | Resend API key; placeholder = email disabled (refused in production) |
| `RESEND_FROM_EMAIL` | ✔ | Sender on a domain verified in Resend (`minastrade.com` today); `resend.dev`/`autobuy.platform` refused |
| `APP_BASE_URL` | ✔ (https) | Only base for links in emails; localhost refused in production |
| `RESEND_REPLY_TO`, `SUPPORT_EMAIL`, `APP_NAME`, `LOG_RESET_LINKS` | optional | Reply-To (falls back to `SUPPORT_EMAIL`), footer address, product name, dev-only link logging (must be 0 in prod) |
| `SUMSUB_APP_TOKEN`, `SUMSUB_SECRET_KEY`, `SUMSUB_WEBHOOK_SECRET` | for KYC | placeholders are treated as unset |
| `RATE_LIMIT_ENABLED`, `MAX_BODY_BYTES`, `LOG_LEVEL` | optional | sensible defaults |

## Email (Resend)

One sender module (`api/integrations/resend.py`), config-only. The verified
Resend domain is **minastrade.com**; `minastech.ca` is not verified and
must only appear as `SUPPORT_EMAIL` / `RESEND_REPLY_TO`, never as the
sender. Production refuses to start or deploy unless `RESEND_API_KEY` is
real, `RESEND_FROM_EMAIL` is on a verified domain, `APP_BASE_URL` is an
`https://` non-localhost origin, and `LOG_RESET_LINKS` is off. To change
domain: verify it in Resend, edit `RESEND_FROM_EMAIL` in the server `.env`,
then `docker compose --profile prod up -d api`. Full variable table and local
testing steps: `docs/API.md` → "Email (Resend)".

## Database

- Schema is **Alembic-only** in production (`alembic upgrade head` runs at container start; `create_all` is dev/test-only).
- Adopting a pre-Alembic DB: `alembic stamp 001 && alembic upgrade head`.
- Backups: `docker exec autobuy_postgres pg_dump -U autobuy autobuy_db | gzip > backup.sql.gz` on a cron; test restores quarterly.
- Restore: `gunzip -c backup.sql.gz | docker exec -i autobuy_postgres psql -U autobuy autobuy_db`.

## Monitoring

Prometheus scrapes `/metrics` (keep it private — nginx config already denies
public access). Thresholds, PromQL sketches, and the runbook: `docs/ALERTING.md`.
`/health` reports per-component state including the scheduler's last tick.

## Scaling

- Vertical: raise gunicorn `--workers` (2×CPU cores is a fine start).
- Horizontal: multiple API containers are safe — the scheduler takes a Postgres
  advisory lock per tick, so replicas never double-execute; rate limits and
  lockout live in shared Redis.
- Put nginx (or a cloud LB) in front for TLS; the app emits HSTS in production.

## Security operations

- Rotation: `SECRET_KEY` (invalidates sessions) and `ENCRYPTION_KEY`
  (run `scripts/rotate_encryption_key.py` with the app stopped) rotate independently.
- Logs are JSON with request ids; secrets never appear (regression-tested).
- Emergency trading stop: `AUTOBUY_DRY_RUN=1` or `SCHEDULER_ENABLED=0` + restart.

## Troubleshooting

| Symptom | Check |
|---|---|
| Boot refuses to start | Production secret guard — set real `SECRET_KEY`/`ENCRYPTION_KEY` |
| 429 on login | Lockout (15 min) or rate limit — `Retry-After` header says which |
| Purchases not executing | `/health` scheduler state; user KYC verified? key verified? `AUTOBUY_DRY_RUN`? |
| "cannot decrypt" on keys | `ENCRYPTION_KEY` changed without running the rotation script |
| Slow requests | JSON logs: `slow_query` warnings; worker count; DB indexes (`EXPLAIN`) |
