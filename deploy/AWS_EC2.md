# Deploying to AWS EC2 (with Tailscale + web UI)

Target: one small Ubuntu EC2 instance (Tokyo `ap-northeast-1` works well for
all supported exchanges) running two containers — the trading bot and the admin web UI — with
the UI reachable **only over your Tailscale network**, never the public
internet. Your VAs manage clients through the UI; API keys stay in a
root-of-server `.env` file and never pass through the UI or its database.

## 1. Instance

- Ubuntu 22.04/24.04, `t3.micro`/`t3.small` is plenty (1 vCPU / 1 GB).
- **Security group inbound: SSH (22) only**, ideally restricted to your IP.
  The web UI does NOT need an open port — Tailscale connects outbound.
- Allocate an **Elastic IP** if you'll IP-whitelist your exchange API keys
  (the default public IP changes on stop/start).

> **Already-provisioned server?** The Tokyo box described in the README is
> set up — day-to-day deploys are just `bash scripts/remote_deploy.sh` from
> your laptop (see "Deploying to the AWS server" in the README). The steps
> below are for provisioning a *new* instance from scratch.

## 2. Install

Preferred auth: a **deploy key**. Generate one on the server
(`ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ''`), add the `.pub` as a
read-only deploy key in the GitHub repo settings, and wire it up in
`~/.ssh/config`:

```
Host github.com
  User git
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
```

Then:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone git@github.com:erwingarcia1/Minas-AI-Asset-Buy---Bitget---Private.git autobuy
cd autobuy && bash deploy/bootstrap.sh
# if told to log out/in: exit, ssh back, re-run  bash deploy/bootstrap.sh
```

## 3. Verify + start

The bot + web UI live in `docker-compose.legacy.yml` (the root
`docker-compose.yml` belongs to the platform API):

```bash
docker compose -f docker-compose.legacy.yml run --rm autobuy --selftest
# PASS lines = server is good. The selftest checks all four exchanges
# (Bitget, Bybit, OKX, Binance) and passes only when every venue is reachable.
docker compose -f docker-compose.legacy.yml up -d
```

Later updates: `bash scripts/deploy.sh` on the server (or
`bash scripts/remote_deploy.sh` from your laptop) — it fast-forwards the
checkout, rebuilds, restarts, and health-checks.

## 4. Tailscale (private access for you + your VAs)

```bash
sudo tailscale up          # prints a URL — log in with your Tailscale account
sudo tailscale serve --bg 8080
tailscale status           # note the machine name
```

The UI is now at `https://<machine-name>.<tailnet>.ts.net` — HTTPS included,
visible only to devices on your tailnet.

**Giving VAs access:**
1. Invite them at https://login.tailscale.com/admin/users (or share an
   invite link). Free plan covers 3 users; the Starter plan covers more.
2. They install the Tailscale app on their computer and log in.
3. They open the UI URL. That's it — no VPN config, no public exposure.

**Recommended:** set `WEB_PASSWORD=...` in `.env` as a second layer
(`sudo docker compose -f docker-compose.legacy.yml up -d webapp` after
changing it — `up -d`, not `restart`, so the new env is actually injected).

## 5. Day-to-day flow

1. A VA opens the UI → **Add client** → fills Name, Email, License #,
   Exchange, Sub Account, Ticker → Save → **Apply to bot**.
2. The entry shows **"awaiting keys"** until you (not the VAs) add that
   sub-account's API credentials to `.env` on the server, e.g. for a
   sub-account named `sub1`:
   ```
   SUB1_API_KEY=...
   SUB1_API_SECRET=...
   SUB1_API_PASSPHRASE=...   # Bitget/OKX only — Bybit/Binance have no passphrase
   ```
   then `sudo docker compose -f docker-compose.legacy.yml up -d autobuy`
   (recreates the container so the new variables are injected — a plain
   `restart` keeps the old environment). Until then the bot simply skips
   that client and keeps trading the others.
3. Config changes applied from the UI are picked up by the bot within one
   poll cycle (~30 s) — no restart needed. Only `.env` changes need the
   container recreated (`up -d`).

## 6. Useful commands

On the Tokyo box docker needs root (`ubuntu` is not in the `docker` group
and `.env` is root-owned), so prefix with `sudo`:

```bash
sudo docker compose -f docker-compose.legacy.yml logs -f autobuy   # watch the bot trade
sudo docker compose -f docker-compose.legacy.yml logs -f webapp    # watch UI activity
sudo docker compose -f docker-compose.legacy.yml up -d autobuy     # after editing .env
sudo docker compose -f docker-compose.legacy.yml down              # stop everything
```

Back up `data/` (client registry + bot config) and `.env` — everything else
is reproducible from the repo.
