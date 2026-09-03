# Sub-Account USDT Auto-Buy (Bitget · Bybit · OKX · Binance)

Watches the **spot USDT balance of each configured exchange sub-account** and,
whenever USDT arrives (deposit, transfer, sale proceeds), automatically places
a **spot market buy** for the coin you picked for that sub-account.

Example: fund `sub1` (Bitget) with USDT → it auto-buys BTC. Fund `sub2`
(OKX) → it auto-buys ETH. Each sub-account picks its own exchange.

Includes an **admin web UI** (client registry: name, email, license #,
exchange, sub-account, ticker) meant to be shared with your team over
**Tailscale** — see [`deploy/AWS_EC2.md`](deploy/AWS_EC2.md) for the full
server setup. VAs add/edit clients and press *Apply to bot*; the bot
hot-reloads the config within one poll cycle. API secrets never enter the
UI or its database — they live only in the server's `.env` file.

## How it works

- Every `poll_seconds` (default 30s) the bot checks each sub-account's
  available spot USDT on that sub-account's exchange.
- If the spendable amount (available minus `reserve_usdt`) is at or above
  `min_usdt_to_trigger` and the symbol's exchange minimum, it market-buys
  the `buy_coin`/USDT pair with the full spendable amount (capped by
  `max_usdt_per_order` if set).
- Order size is floored to the symbol's quote precision, so no rejected-order
  loops; leftover dust below the minimum just waits for more USDT.
- All four exchange clients expose the same normalized interface (balance,
  symbol metadata, quote-denominated market buy, order status), so behavior
  is identical regardless of venue.

The bot derives the trading symbol from `buy_coin` per venue:

| Exchange | Symbol format (for `buy_coin: "BTC"`) |
|---|---|
| Bitget | `BTCUSDT` |
| Bybit | `BTCUSDT` |
| OKX | `BTC-USDT` |
| Binance | `BTCUSDT` |

## Setup

### 1. Create an API key for each sub-account

On every venue the rule is the same: **the API key must be created under the
sub-account it trades** — a main-account key cannot trade inside a
sub-account.

Common to all exchanges:

- Permissions: **Read** + **Spot Trade**. Do **not** grant withdrawal.
- If the venue offers IP restriction (all four do), **allowlist the server's
  IP** — use a stable address (Elastic IP / EIP) so it survives restarts.

Venue specifics:

**Bitget**
- From your **main account**: *Profile → Sub-account management →
  (sub-account) → API management → Create API key*.
- Note the key, secret, and **passphrase** — you need all three.

**Bybit**
- Create the key from the sub-account's own *API Management* page (via
  main account → Subaccounts → the sub-account, or by logging into it).
- No passphrase — just key and secret.
- Works with both **Unified Trading Accounts (UTA)** and legacy classic
  accounts: the bot reads the UNIFIED wallet balance first and falls back
  to the classic SPOT wallet automatically. Just make sure the USDT
  actually sits in the account type your sub-account uses.

**OKX**
- From the main account: *Sub-accounts → (sub-account) → API → Create*.
  You set a **passphrase** at creation — record it, OKX won't show it again.
- **Gotcha: deposits land in the Funding account.** The bot reads the
  **Trading account** balance, so USDT sitting in Funding is invisible to
  it. Configure auto-transfer/sweep into the Trading account (or transfer
  manually) or the bot will never see the deposit.

**Binance**
- From the main account: *Account → Sub-accounts → API management →
  (sub-account) → Create*. Requires a corporate/entity account for
  sub-account support.
- No passphrase — just key and secret.
- **Gotcha: NOTIONAL minimum.** Binance rejects market orders below the
  symbol's NOTIONAL filter (commonly 5 USDT). Keep `min_usdt_to_trigger`
  at or above it; the bot reads the filter and waits rather than spamming
  rejected orders.

### 2. Configure

```bash
cp config.example.json config.json   # config.json is git-ignored
```

Edit `config.json` — one entry per sub-account:

| Field | Meaning |
|---|---|
| `name` | Label used in logs (any string). |
| `exchange` | `bitget` (default), `bybit`, `okx`, or `binance`. Legacy configs without this field keep working unchanged — they mean Bitget. |
| `api_key` / `api_secret` | That sub-account's API credentials. |
| `api_passphrase` | Required for **Bitget and OKX only**; omit for Bybit/Binance. |
| `buy_coin` | Coin to auto-buy with incoming USDT, e.g. `BTC`, `ETH`, `SOL`. The pair traded is the venue's `buy_coin`/USDT symbol (see table above). |
| `min_usdt_to_trigger` | Don't buy until spendable USDT reaches this (default 5). |
| `reserve_usdt` | USDT to always leave untouched in the sub-account (default 0). |
| `max_usdt_per_order` | Cap per single order; `0` = spend everything spendable. |
| `enabled` | Set `false` to pause a sub-account without deleting it. |

Global: `poll_seconds` — how often to check balances.

**Keeping secrets out of the file:** any string value may reference an
environment variable as `${VAR}`, e.g. `"api_key": "${SUB1_API_KEY}"`.
The bot expands these at startup and errors if a referenced variable is
unset. Recommended for any server/container deployment.

The convention (and what the web UI expects) is one variable set per
sub-account, prefixed with the upper-cased sub-account name:

```
SUB1_API_KEY=...
SUB1_API_SECRET=...
SUB1_API_PASSPHRASE=...    # Bitget/OKX only — no passphrase var for Bybit/Binance
```

### 3. Run

```bash
pip install -r requirements.txt

python autobuy.py --dry-run   # recommended first: logs what it WOULD buy
python autobuy.py --once      # single pass, then exit
python autobuy.py             # run forever
```

`python autobuy.py --selftest` (no config or keys needed) checks that the
machine can reach **all four exchanges** — public-API reachability, latency,
market data, and local clock skew (signed requests are rejected if the clock
is off). The selftest passes only when every venue is reachable; if you only
trade some of them, a FAIL line for an unused venue tells you which networks
are blocked but the venues you do use may still work.

### 4. Run it 24/7

**Docker** (config mounted read-only, secrets from `.env`, auto-restart).
The bot + web UI live in `docker-compose.legacy.yml` (the root
`docker-compose.yml` now belongs to the platform API), so always pass `-f`:

```bash
mkdir -p data && cp config.example.json data/config.json
nano data/config.json                                    # use ${VAR} refs
nano .env                                                # VAR=value lines
docker compose -f docker-compose.legacy.yml run --rm autobuy --once --dry-run
docker compose -f docker-compose.legacy.yml up -d
docker compose -f docker-compose.legacy.yml logs -f
```

(The containers read `./data/config.json` — a `config.json` at the repo root
is only used by bare `python autobuy.py` runs.)

**systemd** (no Docker): see `deploy/autobuy.service` — install notes are in
the unit file's header comments.

**Alibaba Cloud**: full ECS walkthrough (region choice, security groups,
EIP whitelisting, both Docker and systemd paths) in
[`deploy/ALIBABA_CLOUD.md`](deploy/ALIBABA_CLOUD.md).

## Deploying to the AWS server (Tokyo EC2)

The production box is an Ubuntu EC2 instance in `ap-northeast-1` running the
bot + admin web UI (and, optionally, the platform API stack). The repo is
checked out at `~/autobuy` on the server, and the server pulls from GitHub
with its own **deploy key** (`~/.ssh/github_deploy`) — no tokens needed.

### SSH into the server

The private key `Minas-AI-Guardian-Execution-Key-Tokyo.pem` sits in the repo
root on your machine (it is gitignored — never commit it, and keep it
`chmod 600`):

```bash
ssh -i Minas-AI-Guardian-Execution-Key-Tokyo.pem \
    ubuntu@ec2-18-179-57-78.ap-northeast-1.compute.amazonaws.com

# or the shortcut, which also runs one-off commands:
bash scripts/server_ssh.sh
bash scripts/server_ssh.sh 'sudo docker ps'
```

### Deploy the latest code — one command from your laptop

```bash
bash scripts/remote_deploy.sh --check    # read-only: show what would change
bash scripts/remote_deploy.sh           # pull + rebuild + restart the live bot
```

This streams `scripts/deploy.sh` over SSH and runs it on the server, so it
works even when the server's checkout predates these scripts.

### Or deploy from a shell on the server

```bash
cd ~/autobuy
bash scripts/deploy.sh            # update the bot + web UI (the live stack)
bash scripts/deploy.sh --check    # read-only preview
bash scripts/deploy.sh platform   # also/instead deploy the platform API stack
bash scripts/deploy.sh all        # both stacks
```

What `deploy.sh` does, in order:

1. Points `origin` at the SSH deploy-key URL (one-time; the box was cloned
   over HTTPS) and fetches.
2. Refuses to deploy a commit that's missing the bot-stack files (catches a
   partial push), then **fast-forward-only** updates the checkout — it also
   refuses if the server has local commits (use `--force` to discard them;
   `.env`, `data/` and `secrets/` are gitignored and never touched either
   way).
3. Deploys the stack you asked for:
   * **bot** — keeps the current image tagged `bitget-autobuy:rollback`,
     builds the new one, smoke-tests it with a `--once --dry-run` pass
     **before** touching the live containers, then restarts bot + web UI and
     watches 30s for crash loops — rolling back to the previous image
     automatically if the new one is unhealthy. The client registry (`data/`)
     and API keys (`.env`) survive every deploy.
   * **platform** — runs the API + Postgres + Redis under a separate compose
     project (`autobuy-platform`) so it can never displace the bot's
     containers; refuses to start unless `SECRET_KEY`/`ENCRYPTION_KEY` are in
     `.env`, and waits for `/health` (migrations apply at boot).
4. Prunes dangling images older than 48h so rebuilds don't fill the disk
   (the tagged rollback image always survives).

### Useful commands on the server

```bash
sudo docker compose -f docker-compose.legacy.yml logs -f autobuy   # watch the bot
sudo docker compose -f docker-compose.legacy.yml logs -f webapp    # watch the UI
sudo docker compose -f docker-compose.legacy.yml up -d autobuy     # after .env edits
sudo docker ps                                                     # everything running
```

Note: after editing `.env` use `up -d` (recreates the container), **not**
`restart` — a plain restart keeps the old environment and your new API keys
would silently never be injected. Config changes applied from the web UI
need neither: the bot hot-reloads `data/config.json` within one poll cycle.

`.env` on the server is root-owned on purpose; edit it with `sudo nano .env`.
Back up `data/` and `.env` — everything else is reproducible from the repo.

## Adding a client via the web UI

1. Open the UI (over Tailscale) → **Add client**.
2. Fill in Name, Email, License #, pick the venue from the **Exchange
   dropdown** (Bitget, Bybit, OKX, or Binance), enter the Sub Account name
   and the Ticker (the coin to buy), then Save.
3. Press **Apply to bot**. The entry shows *awaiting keys* until that
   sub-account's `<SUBACCOUNT>_API_KEY` / `_API_SECRET` (and
   `_API_PASSPHRASE` for Bitget/OKX) are added to the server's `.env`;
   until then the bot skips it and keeps trading the others.

## Tests

No network or credentials needed — every exchange API (Bitget, Bybit, OKX,
Binance) is mocked, with a unit-test suite per client:

```bash
python -m unittest discover -s tests -v
```

## Safety notes

- **Never commit `config.json`** — it holds API secrets (already git-ignored).
- Use API keys with spot-trade permission only; never enable withdrawals.
- Market buys execute at the current market price; on illiquid pairs consider
  `max_usdt_per_order` to split large deposits across polls.
- The bot only ever buys. It never sells, transfers, or withdraws.

---

## On-chain meme buy (v2.2.0)

A separate, manually invoked pipeline that moves USDT off Bitget onto a wallet
you control and swaps it for one specific, pre-allowlisted token on BSC or
Solana.

**The spot bot is untouched.** `autobuy.py` does not import this subsystem, does
not call it, and is not modified by it. There is no hook in the running trade
loop — the only way to invoke the pipeline is to run `memebuy_cli.py` by hand.

Safety posture, in one place:

* dry-run is the default; live mode needs `armed: true` in the config **and**
  the restart-only `MEME_BUY_ARMED=1` **and**
  `--i-understand-this-spends-real-money` on the CLI
* v1 buys **only** tokens whose contract address you allowlisted by hand — there
  is no trending/discovery sniping
* per-trade and rolling-24h USDT caps, persisted in their own SQLite journal, so
  a webapp wipe cannot reset them
* withdrawal destinations come only from the config allowlist
* the kill-switch file is checked before every stage
* every safety check fails closed: missing data is never treated as safe

Two swap backends are selectable per chain: `native` (PancakeSwap V2 on BSC,
Jupiter V2 on Solana) and `bitget` (Bitget Wallet SwapX, which can sponsor gas
and remove the BNB/SOL float). **Both sign with a private key held on this
server** — SwapX is a route provider, not a custody model, so the choice does
not change key-compromise risk. The SwapX backend is **PROVISIONAL: it has never
been run against the live API.** The Bitget app's "Onchain" tab is UI-only and
has no public API, so the withdraw-then-swap flow above is unavoidable. All
three points are covered in detail in `docs/MEME_BUY.md`.

```sh
python3 memebuy_cli.py --status                       # read-only summary
python3 memebuy_cli.py --check-credentials            # which Bitget key files each role has
python3 memebuy_cli.py --check-token bsc 0x...        # safety gate only, spends nothing
python3 memebuy_cli.py --run-once --chain bsc --token 0x... --amount 25 \
    --i-understand-this-spends-real-money             # refuses unless armed
```

**Read [`docs/MEME_BUY.md`](docs/MEME_BUY.md) before configuring or arming
anything.** It covers the pipeline, the owner's manual prerequisite checklist
(Bitget KYC/2FA/elastic IP/withdrawal address book, wallet funding, host
hardening), key handling, every CLI command, the safety gate and its
limitations, failure modes and recovery, and which stages can be undone.

Start from `config.meme_buy.example.json` — it is a **fragment** to be merged
into the real `config.example.json`, not a replacement for it. As written it is
inert: `armed=false`, `dry_run=true`, both chains disabled, both token
allowlists empty.

## Tests

```sh
python3 -m pytest tests/ -q
```
