# Deploying to Alibaba Cloud

The bot is a single long-running process with no inbound traffic — a small
ECS instance is all it needs. Two supported paths: **Docker (recommended)**
or plain **systemd**.

## Before you start: pick the right region

Mainland China regions (Hangzhou, Beijing, Shanghai, Shenzhen, …) generally
**cannot reach the exchange APIs** (Bitget, Bybit, OKX, Binance) — they are
blocked there. Deploy to a region outside the mainland:

- **Hong Kong (`cn-hongkong`)** — close to the exchanges' infrastructure, lowest latency
- **Singapore (`ap-southeast-1`)** — good alternative

Instance sizing: the smallest works. `ecs.t6-c1m1.large` or even
`ecs.t6-c2m1.large` burstable types are more than enough (the bot idles
between 30-second polls). 1 vCPU / 1 GB RAM, 20 GB disk.

## ECS instance setup (both paths)

1. Create the ECS instance (Ubuntu 22.04 or Alibaba Cloud Linux 3) in
   Hong Kong or Singapore with a public IP (or EIP).
2. **Security group**: no inbound rules are needed except SSH (port 22,
   ideally restricted to your IP). Allow all outbound — the bot only makes
   outbound HTTPS calls to the exchange APIs.
3. **Bind an Elastic IP (EIP)** if you plan to IP-whitelist your exchange API
   keys (recommended). The EIP is stable across restarts; whitelist it in
   each sub-account's API key settings.
4. **Time sync**: the exchanges reject requests with skewed timestamps. Alibaba
   ECS images ship with chrony pointed at `ntp.aliyun.com` — verify with
   `chronyc tracking` (offset should be < 1s).

## Path A — Docker (recommended)

```bash
# On the ECS instance
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker

git clone <your-repo-url> autobuy && cd autobuy

# Config: reference secrets as ${VAR} in config.json...
cp config.example.json config.json
nano config.json          # set buy_coin etc.; use "api_key": "${SUB1_API_KEY}", ...

# ...and put the secret values in .env (never committed, never in the image)
cat > .env <<'EOF'
SUB1_API_KEY=...
SUB1_API_SECRET=...
SUB1_API_PASSPHRASE=...   # Bitget/OKX only — Bybit/Binance have no passphrase
EOF
chmod 600 .env

# Dry-run first — confirms credentials, symbols, and connectivity
docker compose build
docker compose run --rm autobuy --once --dry-run

# Go live (restarts automatically on crash and on reboot)
docker compose up -d
docker compose logs -f    # watch it work
```

Log rotation is already configured in `docker-compose.yml` (5 × 10 MB).
To ship logs to Alibaba Log Service (SLS) instead, install the Logtail agent
and point it at the container's json-file logs.

### Optional: build via Alibaba Container Registry (ACR)

If you prefer building the image off-instance, create a (free) personal ACR
repo, then:

```bash
# locally / in CI
docker build -t registry.cn-hongkong.aliyuncs.com/<ns>/bitget-autobuy:latest .
docker login registry.cn-hongkong.aliyuncs.com
docker push registry.cn-hongkong.aliyuncs.com/<ns>/bitget-autobuy:latest
```

and on the instance replace `build: .` in `docker-compose.yml` with
`image: registry.cn-hongkong.aliyuncs.com/<ns>/bitget-autobuy:latest`.
This also unlocks serverless options (SAE / ECI) later — the image has no
local-state requirements beyond the mounted `config.json`.

## Path B — systemd (no Docker)

```bash
sudo apt-get update && sudo apt-get install -y python3-venv git
sudo useradd --system --create-home autobuy

sudo git clone <your-repo-url> /opt/autobuy && cd /opt/autobuy
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt

sudo cp config.example.json config.json && sudo nano config.json
# secrets file read by systemd (referenced as ${VAR} in config.json)
sudo nano /opt/autobuy/autobuy.env      # SUB1_API_KEY=... lines
sudo chmod 600 /opt/autobuy/autobuy.env && sudo chown root:root /opt/autobuy/autobuy.env

# dry-run first
sudo -u autobuy bash -c 'set -a; source /opt/autobuy/autobuy.env; set +a; \
  /opt/autobuy/.venv/bin/python /opt/autobuy/autobuy.py --config /opt/autobuy/config.json --once --dry-run'

sudo cp deploy/autobuy.service /etc/systemd/system/autobuy.service
sudo systemctl daemon-reload
sudo systemctl enable --now autobuy
journalctl -u autobuy -f
```

## Operations checklist

- **First run is always `--once --dry-run`** — it validates credentials and
  symbols and logs what it *would* buy, without spending anything.
- **Go-live test**: transfer a small amount (e.g. 10 USDT) into one
  sub-account and watch the logs for the buy before trusting it broadly.
- **Updating config**: edit `config.json` / `.env`, then
  `docker compose restart autobuy` (or `sudo systemctl restart autobuy`).
- **API keys**: spot-trade + read only, withdrawals disabled, IP-whitelisted
  to the instance's EIP.
- **Monitoring**: Alibaba CloudMonitor can alert on instance down / process
  count; for trade-level alerting, tail the logs for `ERROR` lines
  (`docker compose logs | grep ERROR`).
- Snapshot the disk (or just keep `config.json` + `.env` backed up
  somewhere safe) — everything else is reproducible from the repo.
