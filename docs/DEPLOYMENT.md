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

The compose profile is exactly `prod` (`profiles: ["prod"]` on the api
service). Any other profile name makes `docker compose up -d` a silent no-op:
it prints "Container autobuy_postgres Running", exits 0, and leaves the old
API serving stale code and environment. `scripts/deploy.sh platform` now
refuses a profile that does not include the api service, asserts that the
`autobuy_api` container was actually recreated (id / created-at changed), and
asserts that `/health` reports the checkout's commit (`GIT_COMMIT` build arg
stamped into the image). The API binds `127.0.0.1:8000` only; nginx fronts it
in production and the Vite proxy in development.

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
| 10 | Small-amount canary withdrawals with real money, in the order and shape given in "Go-live runbook" below (XRP first, then BTC) | ⬜ manual | "Go-live runbook" |
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

## Go-live runbook: first real withdrawals (Phase 2)

Status on 2026-09-17: the Tokyo server runs the platform with `AUTOBUY_DRY_RUN=0`
at commit bd93adf and has handled one real transaction (a BTC buy on
2026-09-16, no withdrawal yet). Every step below is deliberate, one at a time,
with a human watching the exchange's own history.

### Step 0 — migration 023 on the server (before any XRP/XLM purchase exists)

Migration 023 (destination type + memo columns, two CHECK constraints) is
committed and applied locally; it is **not** on the server. Apply it as its
own step, never bundled with a trading change:

```bash
# on the server, before anything else: a backup you have listed and can restore
sudo docker exec autobuy_postgres pg_dump -U autobuy -Fc autobuy_db > ~/backups/autobuy_db-before-023-$(date -u +%Y%m%dT%H%M%SZ).dump
ls -la ~/backups | tail -3
scripts/deploy.sh platform            # pulls main, rebuilds, migrates, verifies /health.commit
sudo docker exec autobuy_api alembic current                    # expect 023
sudo docker exec autobuy_postgres psql -U autobuy -d autobuy_db -Atc \
  "select conname from pg_constraint where conname in ('ck_wallets_destination_type','ck_purchases_wallet_destination_type')"
```

Existing rows keep NULL in the new columns; every purchase on the server is
on a tag-free chain, so nothing changes for them.

### Step 1 — XRP first: ~6 XRP to Erwin's own account on another exchange (preferred)

**Why XRP before BTC.** Bitget's XRP minimum withdrawal is 5 XRP, so the test
costs under $15 (about $7 at the 2026-09-17 price) against roughly $41 for the
smallest BTC withdrawal that clears Bitget's minimum plus fee, and the XRP
Ledger settles in seconds rather than the minutes-to-an-hour a BTC withdrawal
takes to confirm.

**Why an exchange address, not a self-custody wallet.** The Bitget withdrawal
request field that carries the destination tag is named `tag` in Bitget's
documentation and SDK and **has never been exercised by this system**. A
self-custody XRP withdrawal carries no tag at all: it would succeed whether or
not the field name is right and prove nothing. Only a withdrawal to an
exchange address *with a tag attached* tests the field name. If the name is
right, the receiving exchange credits the deposit within a minute and the
field is live-verified. If it is wrong, the XRP lands in that exchange's
pooled account, and because it is Erwin's own account and his own tag, he is
the one person who can recover it through that exchange's support. That is
the cheapest possible test of an unverified field name with real money, and
far safer than testing against a third party's address.

Shape of the test:

1. Erwin opens the XRP deposit page of his own account on another exchange
   and copies the deposit **address** and its **destination tag** exactly.
2. On Bitget: the address is saved in the account's withdrawal address book
   (with its tag) and the API key used by AutoBuy has the Withdraw permission
   and the server IP whitelisted. Bitget refuses API withdrawals to unlisted
   addresses.
3. In AutoBuy, a **one-time** XRP purchase, chain `XRP`, answering **"An
   exchange or custody account"** and entering the tag. The form and the API
   both refuse the purchase without it. Size it from the live numbers, not
   from memory: Bitget's minimum withdrawal is 5 XRP and the fee 0.2 XRP on
   top (exclusive mode), so the form's "minimum per buy" for chain `XRP` is
   the floor (about $7 at the 2026-09-17 price of $1.30; a $15 buy sends
   about 11.3 XRP). The purchase amount is a deliberate decision, not a
   default.
4. Watch, in this order: the AutoBuy transaction rows (buy → withdrawal row
   with `withdrawal_memo` = the tag), Bitget's withdrawal history (status,
   tx hash within a minute), then the receiving exchange's deposit history
   (credited to Erwin's account within a minute or two).
5. Record the outcome in `docs/API.md` under "Destination tag / memo": change
   the `tag` field from *unverified* to *verified on <date>, withdrawal
   <Bitget order id>*, and only then remove the "unverified" flag from the
   code comment in `bitget_client.withdraw`. If the deposit is **not**
   credited: do not send again; Erwin opens a ticket with the receiving
   exchange (address + tag + Bitget tx hash), and the field name is corrected
   in `bitget_client.withdraw` before any further tag-chain withdrawal.

### Step 2 — BTC: smallest amount that clears the minimum (~$41)

The first BTC withdrawal also confirms `BITGET_WITHDRAW_FEE_MODE` (exclusive,
the default: AutoBuy sends net minus the 0.00003 BTC fee; if Bitget instead
deducts the fee from the size sent, the wallet receives less than
`expected_received` and the mode must be switched to `inclusive` before the
next run). Use a one-time BTC purchase at the amount the form reports as the
minimum for chain `BTC` (about $41 at current prices: minimum withdrawal
0.0005 BTC plus fee at the live ask), to a self-custody wallet Erwin controls.
Compare the withdrawal row's `amount_executed` / `expected_received` with what
the wallet actually receives and with Bitget's withdrawal record.

### Between steps

`AUTOBUY_DRY_RUN=1` plus a restart stops all real orders at once; the
scheduler pause switch (`POST /api/admin/scheduler/pause`) stops the jobs
without a restart. Each step is one purchase, watched to completion, before
the next.

## Web frontend

The SPA in `frontend/` is static files; it is not part of the API image.
Build it on the server (or in CI) and let nginx serve it:

```bash
cd frontend && npm ci && npm run build          # -> frontend/dist, stamped with dist/BUILD_INFO
sudo rsync -a --delete frontend/dist/ /var/www/autobuy/frontend/dist/
```

Alternatively, the prod compose profile ships a `web` service (nginx, published
on 127.0.0.1:8080) that serves `frontend/dist` straight from the checkout and
forwards `/api/` + `/health` to the `api` container (config:
`deployment/nginx.web.conf`). Build on a laptop, rsync `frontend/dist/` to a
staging directory on the server (for example `~/web-v0.6.1/`) and pass it to
the deploy: `bash scripts/deploy.sh platform --branch main --web ~/web-v0.6.1`.
The script installs it into `frontend/dist` before the API moves and, after
`/health`, checks that nginx on :8080 serves a bundle built from the
checkout's `frontend/` tree (`dist/BUILD_INFO`, written by every
`npm run build`). Without `--web` the deploy refuses to complete unless the
bundle already on disk carries that stamp, so the served SPA can never
silently be an older release than the API (that happened once, with v0.6.0,
before this check existed). Front 8080 with `tailscale serve`
or the host nginx — no Node on the server required.

```bash
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

## Environment variables and docker-compose

The `api` service in `docker-compose.yml` uses an explicit `environment:`
list; docker compose does NOT forward `.env` to the container by itself, so a
variable that is read by `api/config.py` but missing from that list is
silently ignored in production (the container runs on the code default).
Every variable the code reads must therefore appear in the compose list in
`${VAR:-default}` style and in `.env.example`. `tests/test_env_plumbing.py`
compares the three and fails CI on drift in either direction.
