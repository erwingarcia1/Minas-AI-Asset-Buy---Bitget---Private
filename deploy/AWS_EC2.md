# Deploying to AWS EC2 (with Tailscale + web UI)

Target: one small Ubuntu EC2 instance (Tokyo `ap-northeast-1` works well for
Bitget) running two containers — the trading bot and the admin web UI — with
the UI reachable **only over your Tailscale network**, never the public
internet. Your VAs manage clients through the UI; API keys stay in a
root-of-server `.env` file and never pass through the UI or its database.

## 1. Instance

- Ubuntu 22.04/24.04, `t3.micro`/`t3.small` is plenty (1 vCPU / 1 GB).
- **Security group inbound: SSH (22) only**, ideally restricted to your IP.
  The web UI does NOT need an open port — Tailscale connects outbound.
- Allocate an **Elastic IP** if you'll IP-whitelist Bitget API keys
  (the default public IP changes on stop/start).

## 2. Install

SSH in, then (replace `TOKEN` with a GitHub fine-grained read-only token
for this repo):

```bash
sudo apt-get update && sudo apt-get install -y git
git clone -b claude/bitget-subaccount-autobuy-5aut4v \
  https://TOKEN@github.com/erwingarcia1/Minas-AI-Asset-Buy---Bitget---Private.git autobuy
cd autobuy && bash deploy/bootstrap.sh
# if told to log out/in: exit, ssh back, re-run  bash deploy/bootstrap.sh
```

## 3. Verify + start

```bash
docker compose run --rm autobuy --selftest   # PASS lines = server is good
docker compose up -d
```

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
(`docker compose restart webapp` after changing it).

## 5. Day-to-day flow

1. A VA opens the UI → **Add client** → fills Name, Email, License #,
   Exchange, Sub Account, Ticker → Save → **Apply to bot**.
2. The entry shows **"awaiting keys"** until you (not the VAs) add that
   sub-account's API credentials to `.env` on the server, e.g. for a
   sub-account named `sub1`:
   ```
   SUB1_API_KEY=...
   SUB1_API_SECRET=...
   SUB1_API_PASSPHRASE=...
   ```
   then `docker compose restart autobuy`. Until then the bot simply skips
   that client and keeps trading the others.
3. Config changes applied from the UI are picked up by the bot within one
   poll cycle (~30 s) — no restart needed. Only `.env` changes need a
   container restart.

## 6. Useful commands

```bash
docker compose logs -f autobuy     # watch the bot trade
docker compose logs -f webapp      # watch UI activity
docker compose restart autobuy     # after editing .env
docker compose down                # stop everything
```

Back up `data/` (client registry + bot config) and `.env` — everything else
is reproducible from the repo.
