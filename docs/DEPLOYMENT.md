# Deployment & Operations Guide

## Docker

```bash
docker build -f Dockerfile.api -t autobuy-api:latest .   # platform image
docker compose --profile prod up -d --build              # API + Postgres + Redis
docker compose --profile monitoring up -d                # + Prometheus (:9090)
```

The entrypoint applies Alembic migrations, then serves via **gunicorn with 4
uvicorn workers** (measured: p95 245ms @10 concurrent vs 1.6s single-worker).
The root `Dockerfile` belongs to the legacy sub-account bot — don't confuse them.

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
| `RESEND_API_KEY`, `SUMSUB_APP_TOKEN`, `SUMSUB_SECRET_KEY`, `SUMSUB_WEBHOOK_SECRET` | for email/KYC | placeholders are treated as unset |
| `RATE_LIMIT_ENABLED`, `MAX_BODY_BYTES`, `SCHEDULER_POLL_SECONDS`, `LOG_LEVEL` | optional | sensible defaults |

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
