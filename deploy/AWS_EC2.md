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

> **Already-provisioned server?** The Tokyo box described in the README
> (Elastic IP `16.76.92.209`) is set up — day-to-day deploys are just `bash scripts/remote_deploy.sh` from
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
```

## 3. Verify + start

The platform stack (API, Postgres, Redis, nginx web, caddy) is deployed and
verified by `scripts/deploy.sh platform` — see `docs/DEPLOYMENT.md` for the
production checklist, the `.env` validation and the go-live runbook.

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

The platform app is also public at https://autobuy.minastrade.com behind caddy
(see `docs/DEPLOYMENT.md`); Tailscale gives a private second route to it.

## 5. Day-to-day flow and useful commands

Deploys, logs, backups and rollback are described in `docs/DEPLOYMENT.md`;
the SSH shortcut is `bash scripts/server_ssh.sh` (address in
`scripts/lib/server_host.sh`). On this box docker needs root, so prefix
`docker` commands with `sudo`. Back up the Postgres database (`pg_dump`, see
the runbook) and `.env` — everything else is reproducible from the repo.
