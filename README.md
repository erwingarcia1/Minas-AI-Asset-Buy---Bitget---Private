# AutoBuy platform (Bitget)

Automated recurring crypto purchases for customers: a FastAPI + PostgreSQL +
Redis service with a React web app, deployed at
**https://autobuy.minastrade.com**. Customers connect a Bitget API key, pick a
coin, an amount and a cadence, and either accumulate on the exchange or have
every buy withdrawn to their own wallet. Executed buys, withdrawals, holdings
and P&L are recorded from what the exchange actually filled.

Where to look:

- [`docs/API.md`](docs/API.md) — every endpoint, the purchase rules, holdings arithmetic, error codes
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — the production box, `scripts/deploy.sh`, the go-live runbook
- [`SETUP_GUIDE.md`](SETUP_GUIDE.md) — local development
- [`CHANGELOG.md`](CHANGELOG.md) — what shipped, what is verified against the live exchange and what is not
- `clients/` — generated Python, JavaScript and TypeScript SDKs from the committed OpenAPI spec
- `frontend/public/logos/` — coin logos served from our own domain; `LOGO_SOURCES.md` there lists each file, source and licence and drives the `/logo-credits` page

The legacy sub-account auto-buy bot, its Flask admin UI and the on-chain
meme-buy subsystem were removed from this repository on 2026-09-22 (tag
`v0.6.5-pre-audit` is the last commit that carries them). Only the exchange
client modules at the repo root remain, because the platform uses them.

## Deploying to the AWS server (Tokyo EC2)

The production box is an Ubuntu EC2 instance in `ap-northeast-1` running the
platform stack (API, Postgres, Redis, nginx web, caddy). The repo is
checked out at `~/autobuy` on the server, and the server pulls from GitHub
with its own **deploy key** (`~/.ssh/github_deploy`) — no tokens needed.

The instance has an **Elastic IP, `16.76.92.209`**, so its public address
survives a stop/start. Use that address everywhere a fixed one is needed: SSH
(`scripts/server_ssh.sh` and `scripts/remote_deploy.sh` default to it), the
exchange API keys' IP allowlists, and the DNS A record for the public
hostname `autobuy.minastrade.com`. The old auto-assigned address
(`18.179.57.78` / `ec2-18-179-57-78…`) was released with the association and
no longer reaches this box.

**All deployments come from the `main` branch.** Both the server and your
local clone are checked out on `main`; `deploy.sh` deploys whatever branch
the server has checked out (override with `--branch NAME` for a one-off).
Merge to `main`, push, then deploy.

The platform web app is public at **https://autobuy.minastrade.com** (the
`caddy` compose service terminates TLS with an automatic Let's Encrypt
certificate — see "Public HTTPS" in `docs/DEPLOYMENT.md`). The same app is
also reachable privately at `https://ip-172-31-21-173.tail0fdd4a.ts.net` for
members of the Tailscale network (invite people at
https://login.tailscale.com/admin/users).

### SSH into the server

The private key `Minas-AI-Guardian-Execution-Key-Tokyo.pem` sits in the repo
root on your machine (it is gitignored — never commit it, and keep it
`chmod 600`):

```bash
ssh -i Minas-AI-Guardian-Execution-Key-Tokyo.pem ubuntu@16.76.92.209

# or the shortcut, which also runs one-off commands:
bash scripts/server_ssh.sh
bash scripts/server_ssh.sh 'sudo docker ps'
```

### Deploy the latest code — one command from your laptop

```bash
bash scripts/remote_deploy.sh --check    # read-only: show what would change
bash scripts/remote_deploy.sh           # pull + rebuild + restart the platform
```

This streams `scripts/deploy.sh` over SSH and runs it on the server, so it
works even when the server's checkout predates these scripts.

### Or deploy from a shell on the server

```bash
cd ~/autobuy
bash scripts/deploy.sh platform --branch main --web ~/web-vX.Y.Z   # the normal deploy (see docs/DEPLOYMENT.md)
bash scripts/deploy.sh --check                                     # read-only preview
```

What `deploy.sh` does, in order:

1. Keeps `origin` on the SSH deploy-key URL and fetches the deploy branch
   (`main`, unless `--branch` says otherwise).
2. **Fast-forward-only** updates the checkout — it also
   refuses if the server has local commits (use `--force` to discard them;
   `.env`, `data/` and `secrets/` are gitignored and never touched either
   way).
3. Deploys the platform: checks the server `.env`, validates compose, installs
   or verifies the web bundle (`--web DIR`, or a matching
   `frontend/dist/BUILD_INFO`), rebuilds the API image stamped with the
   commit, and refuses to report success unless the API container was
   recreated, `/health` reports that commit, and nginx serves the bundle built
   from this checkout. Migrations apply at boot. Before the first deploy, work
   through the **production checklist** in
   [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md): fill `.env` from
   `.env.production.example` and check it with
   `python scripts/validate_env.py .env` on the server.
4. Prunes dangling images older than 48h so rebuilds don't fill the disk
   (the tagged rollback image always survives).

### Useful commands on the server

```bash
sudo docker ps                                                     # everything running
```

(and rolls back to it automatically if the new one crash-loops). To roll
back by hand:

```bash
```

Note: after editing `.env` use `up -d` (recreates the container), **not**
`restart` — a plain restart keeps the old environment and your new API keys
would silently never be injected. Config changes applied from the web UI

`.env` on the server is root-owned on purpose; edit it with `sudo nano .env`.
Back up `data/` and `.env` — everything else is reproducible from the repo.


## Tests

```bash
python3 -m pytest tests/ -q
```

Tests never touch `autobuy_db`. `tests/conftest.py` points the suite at a
throwaway **`autobuy_test`** database (created and migrated to Alembic head
automatically) and **Redis db 1**, refuses to run against any database whose
name does not end in `_test` or any other Redis db, and disables `.env`
loading so real keys and senders on a dev box can never reach a test.
Override the targets with `TEST_DATABASE_URL` / `TEST_REDIS_URL`; the same
guards apply. CI (`.github/workflows/api-tests.yml`) runs the full suite the
same way with an 85 % coverage gate on `api/`.
