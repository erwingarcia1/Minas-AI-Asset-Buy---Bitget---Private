# Bitget Sub-Account USDT Auto-Buy

Watches the **spot USDT balance of each configured Bitget sub-account** and,
whenever USDT arrives (deposit, transfer, sale proceeds), automatically places
a **spot market buy** for the coin you picked for that sub-account.

Example: fund `sub1` with USDT → it auto-buys BTC. Fund `sub2` → it auto-buys ETH.

## How it works

- Every `poll_seconds` (default 30s) the bot checks each sub-account's
  available spot USDT.
- If the spendable amount (available minus `reserve_usdt`) is at or above
  `min_usdt_to_trigger` and the symbol's exchange minimum, it market-buys
  `<buy_coin>USDT` with the full spendable amount (capped by
  `max_usdt_per_order` if set).
- Order size is floored to the symbol's quote precision, so no rejected-order
  loops; leftover dust below the minimum just waits for more USDT.

## Setup

### 1. Create an API key for each sub-account

Bitget only allows trading inside a sub-account with that sub-account's own
API key. From your **main account**: *Profile → Sub-account management →
(sub-account) → API management → Create API key*.

- Permissions: **Read** + **Spot Trade**. Do **not** grant withdrawal.
- Note the key, secret, and passphrase — you need all three.
- If you restrict by IP, add the IP of the machine running this bot.

### 2. Configure

```bash
cp config.example.json config.json   # config.json is git-ignored
```

Edit `config.json` — one entry per sub-account:

| Field | Meaning |
|---|---|
| `name` | Label used in logs (any string). |
| `api_key` / `api_secret` / `api_passphrase` | That sub-account's API credentials. |
| `buy_coin` | Coin to auto-buy with incoming USDT, e.g. `BTC`, `ETH`, `SOL`. The pair traded is `<buy_coin>USDT`. |
| `min_usdt_to_trigger` | Don't buy until spendable USDT reaches this (default 5). |
| `reserve_usdt` | USDT to always leave untouched in the sub-account (default 0). |
| `max_usdt_per_order` | Cap per single order; `0` = spend everything spendable. |
| `enabled` | Set `false` to pause a sub-account without deleting it. |

Global: `poll_seconds` — how often to check balances.

### 3. Run

```bash
pip install -r requirements.txt

python autobuy.py --dry-run   # recommended first: logs what it WOULD buy
python autobuy.py --once      # single pass, then exit
python autobuy.py             # run forever
```

Keep it running 24/7 with systemd, Docker, `tmux`, or any process manager, e.g.:

```bash
nohup python autobuy.py >> autobuy.log 2>&1 &
```

## Safety notes

- **Never commit `config.json`** — it holds API secrets (already git-ignored).
- Use API keys with spot-trade permission only; never enable withdrawals.
- Market buys execute at the current market price; on illiquid pairs consider
  `max_usdt_per_order` to split large deposits across polls.
- The bot only ever buys. It never sells, transfers, or withdraws.
