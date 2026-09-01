# AutoBuy Crypto Platform — Setup Guide

Setup for the FastAPI platform in `api/`. (The legacy sub-account bot has its own
setup in [README.md](README.md).)

## Prerequisites

- Python 3.12, Docker + docker-compose
- `cp .env.example .env` and fill in values (`SECRET_KEY` at minimum)

## Run the dev stack

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
docker-compose up -d                  # Postgres 16 + Redis 7
./venv/bin/alembic upgrade head       # apply database migrations
./venv/bin/uvicorn api.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs · Health: http://localhost:8000/health

## Database migrations (Alembic)

The schema is versioned with Alembic; migration scripts live in `alembic/versions/`.
`alembic/env.py` reads `DATABASE_URL` from the environment / `.env` (the async
`+asyncpg` driver is swapped for `+psycopg2` automatically for migration runs).

**Apply all pending migrations:**

```bash
alembic upgrade head
```

**Create a new migration after changing `api/models.py`:**

```bash
alembic revision --autogenerate -m "describe the change"
# then REVIEW the generated file in alembic/versions/ before applying —
# autogenerate misses data migrations and names some constraints poorly
alembic upgrade head
```

**Check where the database is:**

```bash
alembic current      # revision the DB is at
alembic history      # all known revisions
```

**Roll back one revision:** `alembic downgrade -1`

### Existing databases (created before Alembic)

A database that was created by the old `create_all` startup path already has the
baseline tables. Adopt it into versioning with:

```bash
alembic stamp 001    # mark baseline as applied without running it
alembic upgrade head # apply the real fixes (002+)
```

### Environments

- **Development/tests:** app startup still runs `create_all` as a convenience
  (new tables appear without a migration), but committed schema changes must
  ship as migrations.
- **Production (`API_ENV=production`):** startup does **not** touch the schema.
  Run `alembic upgrade head` as a release step before starting the app.

### Notable revisions

| Revision | What it does |
|---|---|
| `001` | Baseline: users, api_keys, recurring_purchases, transactions |
| `002` | `transactions.recurring_purchase_id` FK → `ON DELETE SET NULL` (deleting a schedule no longer destroys its transaction history) · all datetime columns → `timestamptz` (existing values interpreted as UTC) · index on `transactions (user_id, created_at)` |
