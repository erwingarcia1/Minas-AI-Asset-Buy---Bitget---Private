# On-chain meme buy (v1)

The meme-buy subsystem moves USDT off Bitget onto a wallet you control and swaps
it for one specific token on BSC or Solana. It is a separate, manually invoked
pipeline: `autobuy.py` does not import it, does not call it, and is not modified
by it. Deploying these packages cannot change what the live spot bot does.

Everything below is about software behaviour, operational risk and failure
modes. There is nothing here about which tokens to buy or whether to buy any.

---

## 1. What it does, and what it deliberately does not

### Pipeline

```
0  select        candidate from the CONFIGURED ALLOWLIST
1  transfer      sub-account -> main-account USDT transfer   (reversible)
2  withdraw      on-chain withdrawal to your wallet          <-- IRREVERSIBILITY BOUNDARY
3  arrival       poll the destination wallet until funds land (bounded timeout)
4  gate          safety.gate.screen_token, point-in-time
5  swap (BSC)    PancakeSwap V2, swapExactTokensForTokensSupportingFeeOnTransferTokens
6  swap (Solana) Jupiter Swap API V2, order -> sign -> execute
7  outcome       journal the result
```

The kill switch is checked before *every* stage, including stage 0. Every stage
writes a `begin` event to the SQLite journal before it acts and an `end` event
after it returns, so a crash leaves a record of exactly how far things got.

Packages:

| Package         | Owns                                                                 |
| --------------- | -------------------------------------------------------------------- |
| `memebuy/`      | sequencing, config validation, caps, kill switch, SQLite journal      |
| `payout/`       | the Bitget transfer/withdraw/status calls (see the contract note below) |
| `chains/`       | RPC clients, swap execution, file-backed signing keys                 |
| `safety/`       | the fail-closed pre-trade token screen and its data-source clients    |
| `memebuy_cli.py`| the only entrypoint; there is no hook in the trade loop               |

`memebuy/orchestrator.py` imports its siblings **lazily, inside functions**. A
missing or broken sibling is therefore a journaled, fail-closed abort rather
than a crash at process start. At the time of writing `payout/` is consumed
purely through that contract:

```
payout.bitget_payout.BitgetPayout(account_role="sub"|"main")
    .transfer_sub_to_main(coin, amount, client_oid, dry_run)      SUB key only
    .withdraw_onchain(coin, chain, address, amount, client_oid,   MAIN key only
                      allowlist, dry_run)
    .get_withdrawal_status(client_oid, start_ms, end_ms)
```

Two instances are built, from two different env-var prefixes, and they are not
interchangeable — see §3.

If the module is absent, `--run-once` aborts at stage 1 with `safety gate
unavailable` / an import error in the journal, and no money moves.

### v1 buys ONLY what you allowlisted

There is no trending feed, no new-pair scanner, no social scraper, no discovery
of any kind. The bot buys a token if and only if its contract address appears in
`meme_buy.chains.<chain>.token_allowlist` in the config, *and* in the separate
`safety` allowlist file named by `SAFETY_ALLOWLIST_FILE`. Both lists are
checked; the config list gates stage 0, the safety list is check 01 of the gate.

`armed: true` with an empty `token_allowlist` is a hard config error at load
time, not a warning:

```
meme_buy.chains.bsc.token_allowlist is empty while armed=true
(v1 buys only from the configured allowlist)
```

Why it was built this way:

* **Discovery is the part that loses money, and it is unfalsifiable in a test
  suite.** Every check in `safety/gate.py` can be exercised against recorded
  fixtures. "Is this trending token a good target" cannot. Shipping selection
  logic would mean shipping the one component whose failure rate is unknown to
  everyone including its author.
* **Sniping is a latency race the bot cannot win.** The pipeline's own critical
  path includes a CEX withdrawal that takes minutes. Anything whose edge decays
  in blocks is structurally out of reach; pretending otherwise would only make
  the bot a reliable exit for someone else's fill.
* **An allowlist makes every buy attributable to a human decision.** There is a
  named person who typed a contract address into a file. That is the audit trail
  the caps, the journal and the kill switch are all built around.
* **It bounds the blast radius of a bug.** The worst outcome of a defect in this
  code is buying the wrong amount of a token you already chose — not buying an
  arbitrary contract the code discovered on its own.

Naming a token by symbol is impossible on purpose. `chains/solana_swap.py`
rejects anything shorter than 32 characters with "Tokens are never resolved by
symbol"; a live mint exists whose symbol renders as `USDT` using Cyrillic
homoglyphs. Identity is the address.

---

## 2. The owner's manual prerequisite checklist

None of this can be done by code. All of it has to be true before `--run-once`
will do anything useful, and several items fail *silently* or *irreversibly* if
skipped. Work top to bottom.

### Bitget — the two API keys, one checklist each

The pipeline needs **two different Bitget API keys**. They are not
interchangeable and neither can do the other's job. Provision them separately,
tick them separately, and store them under separate env-var prefixes.

**Key 1 — SUB key (stage 1: sub → main transfer)**

* [ ] Created **under the sub-account**, from the main account:
      *Profile → Sub-account management → (sub-account) → API management*.
* [ ] Permission: **Transfer only.** Nothing else is needed for stage 1.
* [ ] **A sub-account key structurally CANNOT withdraw — whatever is ticked in
      the UI.** Bitget only ever grants sub keys `uta_trade` / `uta_mgt`. Ticking
      "Withdraw" on a sub key does not enable it; the call fails at the exchange.
      This is why stage 1 exists at all. `payout/bitget_payout.py` also refuses
      the wrong role locally, before any network call.
* [ ] Record key + secret + **passphrase** (Bitget issues all three).

**Key 2 — MAIN key (stage 2: the irreversible on-chain withdrawal)**

* [ ] Created on the **main account**, not a sub-account.
* [ ] Permissions: **Withdraw + Read.** Read is needed for the per-`clientOid`
      withdrawal-status lookup that makes resume safe. Do **not** grant Trade.
* [ ] **IP-allowlisted** to this host, and to a *stable* address — see the next
      item, which is the one that actually bites.
* [x] **The EC2 box has an Elastic IP: `16.76.92.209`.** Lock the key to *that*
      address. (Until 2026-09-21 the box had the auto-assigned `18.179.57.78`,
      which a stop/start would have replaced; an allowlist still naming it
      makes **every withdrawal call fail with error 40038** while spot trading
      keeps working, so nothing looks broken.) If the Elastic IP is ever
      disassociated or the instance replaced, update the allowlist first.
* [ ] **Destination addresses added BY HAND** to the Bitget withdrawal address
      book, one per chain, before the first run. **There is no API to add a
      withdrawal address** (the API is read-only: `GET
      /api/v3/account/withdraw-address`). That is a security feature, not an
      omission. An address that is not in the book cannot be withdrawn to, no
      matter what the config says.
* [ ] **No security-setting changes for 24 hours before going live.** Changing
      the password, 2FA binding, email or phone freezes withdrawals for 24h;
      attempts during the window return **error 40052**. Do the security work
      first, then wait a day, then arm.
* [ ] **Main-account KYC completed.** Withdrawals are gated on it; sub-account
      KYC does not substitute.
* [ ] **2FA bound** on the main account (Google Authenticator / equivalent).
      Bitget requires it for withdrawal-permitted keys.

**Both keys**

* [ ] Each of the six secrets lives in **its own `0600`, root-owned file**, and
      the environment variable holds the **PATH** to that file — never the secret
      itself:

      BITGET_SUB_API_KEY_FILE          BITGET_MAIN_API_KEY_FILE
      BITGET_SUB_API_SECRET_FILE       BITGET_MAIN_API_SECRET_FILE
      BITGET_SUB_API_PASSPHRASE_FILE   BITGET_MAIN_API_PASSPHRASE_FILE

* [ ] Verified with **`python3 memebuy_cli.py --check-credentials`** (§4) — it is
      read-only, offline, and never opens the files, so it cannot leak a secret
      into your terminal or a CI log. A live run refuses to start without both
      key sets anyway; finding out here costs nothing.
* [ ] Rotate the API key that was pasted into chat during the original
      deployment. Treat it as public.

### Wallets

* [ ] **Generate both wallets offline, on a machine that is not the EC2 box.**
      The server should only ever see the key file, never the generation
      process, and never the seed phrase.
* [ ] **Fund the BSC wallet with >= 0.01 BNB for gas.** Gas is paid in BNB; a
      USDT-only wallet cannot even send the ERC-20 approve. `chains/bsc_swap.py`
      hard-fails below its floor with `BNB_FLOOR` before doing anything else.
* [ ] **Fund the Solana wallet with 0.03–0.05 SOL — not 0.01.** The ATA rent for
      the first-ever buy of a given token is `0.00203928 SOL` on its own and is
      not refunded while the account exists; add the base fee and a priority fee
      and 0.01 SOL does not cover a first buy. The module's default floor is
      0.04 SOL and it clamps a lower configured floor *upward*.
* [ ] **Record a cold sweep address for each chain**, written down off-machine.
      Nothing in this subsystem sells or sweeps; the sweep address is what you
      need on the day you move a position out by hand.

### Host

* [ ] **Enforce IMDSv2** on the instance (`HttpTokens=required`). With IMDSv1
      still enabled, any SSRF or a compromised dependency can read the instance
      role credentials from `169.254.169.254` with a single unauthenticated GET.
* [ ] **Set `WEB_PASSWORD`.** It is still unset on the deployed box. The UI is
      tailnet-only, which is a network control, not an authentication one.
* [ ] Key files `0600`, owned by root, outside the repo and outside any Docker
      build context.

---

## 3. Key handling

### The `*_KEY_FILE` convention

House rule, shared with the CEX clients: **a private key is never read from a
plain environment variable.** The environment variable holds a *path*; the code
opens the file.

```sh
export BSC_PRIVATE_KEY_FILE=/run/secrets/bsc.key       # 0600, root-owned
export SOLANA_PRIVATE_KEY_FILE=/run/secrets/solana.key # 0600, root-owned
export SOLANA_RPC_URL_FILE=/run/secrets/solana_rpc.url # paid RPC URLs embed keys
export SAFETY_ALLOWLIST_FILE=/etc/minas/allowlist.json # a path, not a secret
export MEME_BUY_ARMED=1                                # arming half; see §4
```

### The two Bitget API keys

A Bitget sub-account key **cannot withdraw** — the exchange only grants sub keys
`uta_trade`/`uta_mgt`. So the pipeline needs two different keys, and the same
`*_FILE` convention applies to both:

```sh
# stage 1: sub -> main transfer. Signed with the sub-account's OWN key.
# Permission: TRANSFER only. A sub key cannot withdraw whatever is ticked.
export BITGET_SUB_API_KEY_FILE=/run/secrets/bitget_sub.key
export BITGET_SUB_API_SECRET_FILE=/run/secrets/bitget_sub.secret
export BITGET_SUB_API_PASSPHRASE_FILE=/run/secrets/bitget_sub.pass

# stage 2: the IRREVERSIBLE on-chain withdrawal. Main-account key.
# Permissions: WITHDRAW + READ, IP-allowlisted to this host (else 40038).
export BITGET_MAIN_API_KEY_FILE=/run/secrets/bitget_main.key
export BITGET_MAIN_API_SECRET_FILE=/run/secrets/bitget_main.secret
export BITGET_MAIN_API_PASSPHRASE_FILE=/run/secrets/bitget_main.pass
```

Every one of those six variables holds a **PATH** to a `0600`, root-owned file
containing that single secret — never the secret itself. See §2 for the
provisioning checklist and the blast-radius note later in this section for what
the main key is actually worth to an attacker.

The `BITGET_SUB` / `BITGET_MAIN` prefixes are the defaults; change them with
`meme_buy.bitget_sub_prefix` / `bitget_main_prefix` in the config. The two must
differ — one key playing both roles is exactly what this model prevents.

`payout/bitget_payout.py` refuses the wrong role locally, before any network
call, and `memebuy/orchestrator.py` builds two separate clients and checks the
role of each before it calls it. A **live** run also verifies both key sets are
present and readable at stage 0: discovering the main key missing at stage 2
would leave the USDT parked in the main account with a half-finished run behind
it. Verify by hand at any time with `--check-credentials` (§4).

### Swap backends

Each chain picks who executes the swap once the USDT has landed in your own
wallet, via `meme_buy.chains.<chain>.swap_backend`. Any other value is a hard
config error.

> **The load-bearing fact, before the table:** **both backends still sign with a
> private key held on this server.** SwapX is a *route provider*, not a custody
> model — it returns unsigned transaction data and this process signs it with the
> key loaded from `BSC_PRIVATE_KEY_FILE` / `SOLANA_PRIVATE_KEY_FILE`. Bitget
> never holds the key and never holds the funds. **Switching `swap_backend` does
> not reduce key-compromise risk by any amount.** Anyone who can read the key
> file can drain the wallet under either backend, exactly the same way.

| | `native` (default) | `bitget` |
|---|---|---|
| Router | PancakeSwap V2 on BSC (`chains/bsc_swap.py`), Jupiter Swap API V2 on Solana (`chains/solana_swap.py`) | Bitget Wallet Trading API "SwapX" aggregator (`chains/bitget_swap.py`) |
| Chains | `bsc`, `solana` | `bsc`, `solana` (chain index `56` / `501`) |
| API key needed? | **No.** Public router contract + public Jupiter API. Only an RPC URL. | **Yes.** Apply at <https://portal-web3.bitget.com>. The application is **not** self-serve end to end — it has a BD/contact step, so budget calendar time, not minutes. Keys are passed as `SecretKey` objects; the client never sees raw strings. |
| Gas | Wallet pays its own. Keep `min_native_balance` funded: **≥ 0.01 BNB** on BSC, **0.03–0.05 SOL** on Solana (ATA rent for a first-ever buy is 0.00203928 SOL alone). Enforced by `BNB_FLOOR` / `SOL_FLOOR` before anything is signed. | Same as `native` **unless** `gasless: true`. With gasless the aggregator sponsors gas (EIP-7702-style delegation on EVM — you sign EIP-712 typed data instead of a transaction; an internal fee payer on Solana). Requires a **stablecoin input** and an order of **≈ \$5 minimum** (`GASLESS_MIN_USDT`). |
| Native-balance enforcement | Enforced — the module holds an RPC handle. | **Not enforced.** SwapX holds no node of our own; the result reports `native_balance_checked: false`. Non-gasless means the caller keeps that duty. `min_native_balance` set without `gasless` only logs a warning. |
| Tax / fee-on-transfer tokens | BSC always uses `swapExactTokensForTokensSupportingFeeOnTransferTokens`, so a taxed token does not revert `Pancake: K`. Budget the buy tax with `buy_tax_bps` or the swap fails `INSUFFICIENT_OUTPUT_AMOUNT`. | Handled by the aggregator's own routing; there is no local `buy_tax_bps` equivalent. The order's `minReceiveAmount` is compared against the amount our quote authorised and the swap is **refused before signing** if the order protects less (`MIN_RECEIVED`). |
| Verified against the live API? | Yes. | **No — see the warning below.** |

`gasless: true` with `swap_backend: "native"` is **rejected at config load**, not
ignored — silently dropping it would leave you believing a wallet needs no
BNB/SOL, and the swap would then fail *after* the irreversible withdrawal.

Whichever backend is configured, the orchestrator still refuses to call a
`buy_token` that does not take `dry_run` as a real named parameter, so switching
routers is not a way around the guard.

### PROVISIONAL — the SwapX backend has never met the live API

> ### ⚠️ `swap_backend: "bitget"` IS UNVERIFIED
>
> `chains/bitget_swap.py` was **written to the published documentation only**. It
> has **never been executed against the live Bitget Wallet Trading API.** No
> request in it has ever received a real response, so **every endpoint path,
> request/response field name, envelope shape and signing detail in it is
> provisional** and must be confirmed on first use. The results it returns carry
> `unverified_integration: true` to make this visible downstream.
>
> **Do not arm this backend and walk away.** Exercise it with `--dry-run` first —
> the dry-run path stops after the read-only `quote` call, before `makeSwapOrder`
> and before anything is signed, and it returns the exact `intended_call` it
> would have made.

Where to correct it, so you are not searching:

* **If authentication fails** (401/403, a signature-rejected code, or a "missing
  header" message): the entire HMAC recipe lives in **one method,
  `BitgetSwapClient._auth_headers`** — header names (`HEADER_API_KEY`,
  `HEADER_SIGN`, `HEADER_TIMESTAMP`), the digest encoding (`SIGN_ENCODING`,
  hex vs base64), and the prehash component order (`SIGN_PARTS`, currently
  timestamp + METHOD + path + body, modelled on Bitget's CEX v2 scheme). **Fix
  `_auth_headers` and nothing else.** No call site assembles headers.
* **If a field is missing or misread**, correct the `WIRE` dict, not the call
  sites. Every provisional field name is an alias tuple there; the first alias is
  what we send and reads try each in order.
* **If a path 404s**, correct `PATHS`. **If the host is wrong**, pass `base_url`.
* **If the chain id is wrong**, correct `CHAIN_INDEX` (one place).

There is a second host worth knowing about: `https://copenapi.bgwapi.io` is what
Bitget's own MIT-licensed tools (bitget-wallet-mcp / -cli / -skill) call with
**no API key**. It is deliberately **not** the default — it is a client-facing
endpoint that may change without notice, and its terms-of-service position for
unattended bot use is unclear.

Fail-closed behaviour is already in place regardless: a non-JSON body, a
non-object `data` member, an absent `data` member, or a business-code failure
inside an HTTP 200 all raise rather than being read as success. The caller
contract is the one that matters operationally — an exception with
`exc.signed is False` was raised **before any signature existed** (definitely not
sent, safe to retry); `exc.signed is True` means a signature existed, and
`BitgetSwapUnconfirmed` means **reconcile `exc.order_id` before any retry or you
may buy twice**.

### What Bitget "Onchain" is NOT

The Bitget **app** has an "Onchain" tab that buys BSC and Solana memecoins
straight from your spot USDT — no self-custody wallet, no withdrawal, no gas.
That flow is **UI-only. There is no public API for it.** None.

* The Bitget exchange API exposes exactly four permission scopes — **Read,
  Trade, Transfer, Withdraw** — and has **no onchain / DEX / Web3 section** of
  any kind. There is no endpoint that spends a *spot balance* on a DEX token.
* Bitget Wallet SwapX (the `bitget` backend above) is a **different product**. It
  is a self-custody DEX aggregator: it cannot see, touch or spend a Bitget
  exchange balance, and it is not a CEX order route.
* **No major exchange offers the in-app onchain flow through a documented API.**
  This was checked; it is not a Bitget-specific gap.

The consequence is structural, not a design preference: **the
withdraw-then-swap flow this pipeline implements is unavoidable.** The
irreversible on-chain withdrawal at stage 2 cannot be engineered away by picking
a different backend or a different exchange. If you find yourself wishing the bot
could "just buy it the way the app does" — it cannot, and neither can anything
else that is automatable.

`chains/wallet_keys.py` is the only key loader in the package — do not add a
second one. It strips trailing whitespace (a `echo >` newline or a k8s secret
mount), rejects multi-line files, validates the format (`evm` = 32-byte hex,
`base58` = Solana secret key), warns if the file is group/world readable, and
wraps the material in `SecretKey`.

`SecretKey.__repr__`, `__str__` and `__format__` all return `<redacted>`, so the
key cannot leak through a log line, an f-string, a `%s`, or a traceback frame
that renders its arguments. It is not picklable and not copyable, and defines
neither `__eq__` nor `__hash__` so the value cannot be probed by comparison. The
raw material is reachable only via an explicit `.reveal()` — keep that call
inline at the signing site and never bind it to a local that outlives the
statement.

One deliberate quirk: when the key file cannot be read, the error reports the
errno and the *variable name*, never the path. The classic misconfiguration is
pasting the key into the variable, at which point the "path" in the `OSError`
message **is** the key.

The orchestrator never holds key material at all. It passes no key and no web3
handle to `chains/`; each swap module resolves its own key from its own `*_FILE`
variable at the signing site. A Solana dry run with `taker` supplied explicitly
never opens the key file.

### Why environment variables leak

An env var is not a secret store. It is readable, in plaintext, from more places
than most people check:

* `docker inspect <container>` prints the full environment to anyone in the
  `docker` group — which is root-equivalent.
* `/proc/<pid>/environ` is readable by the process owner and by root, and
  survives into anything that reads it: a crash handler, an APM agent, a `ps`
  wrapper.
* **Every child process inherits it.** A shell-out, a subprocess, a plugin, a
  `pip` post-install hook — all of them get the key.
* Compose renders it: `docker compose config` and the generated `config.v2.json`
  can end up with the value written to disk, in the repo, or in a backup.
* Exception reporters and log formatters that dump "environment" on error will
  cheerfully upload it.

A file has one owner, one mode, and one place to audit. That is the whole
argument.

### Blast radius — what a compromised host actually costs

State it plainly, because the two-key model is often read as being *smaller*
than it is:

**A main-account key with Withdraw permission means a compromised host can drain
the exchange balance — not just the hot wallet.** An attacker with code
execution on the box does not need to steal anything: the credential files are
readable to the process by design, and the withdrawal endpoint is a single
authenticated call. The exposure is not "the ~\$25 sitting in the BSC wallet". It
is **whatever is in the Bitget main account**, up to whatever the exchange lets
that key move.

Nothing in this repository can change that. The mitigations are exchange-side
and infrastructure-side:

* **Exchange-side address whitelisting** — the withdrawal address book is
  manual-only and there is no API to add an entry, so a compromised host can only
  send funds to an address *you* already wrote down. This is the single most
  valuable control in the whole design, and it is a Bitget feature, not ours.
  Keep exactly one address per chain in the book.
* **A withdrawal cap set on the exchange**, not just `daily_usdt_cap` in the
  config. Our cap lives in a SQLite file on the same host the attacker owns; the
  exchange's cap does not.
* **Keep the main account's balance small.** A key that can withdraw everything
  is only as dangerous as "everything" is.

### The upgrade path that survives a compromised host

**A remote signer that enforces policy at signing time.** The key never exists on
the instance; the instance asks for a signature and the *signer* decides. If the
policy says "USDT → allowlisted router only, ≤ \$X per day", a host with full
code execution still cannot sign anything else.

* **Turnkey** and **Coinbase CDP server wallets** both do this today, both are
  **self-serve** (no sales call to get started), both cover **EVM and Solana**,
  and both support **spend caps and router/contract allowlists enforced at the
  signing boundary**. That is the property that matters: the guardrail is on the
  other side of the compromise.
* `chains/wallet_keys.py` is the single seam where any of this is introduced —
  it is the only key loader in the package, and the swap modules resolve their
  key through it at the signing site. Do not add a second loader.

**AWS KMS is a weaker answer, and it is worth being precise about why.** As of
November 2025 KMS supports both secp256k1 and Ed25519, so it covers BSC and
Solana with one service, and it genuinely stops **key theft** — the private key
never exists as a file, and every signature lands in CloudTrail. But **KMS signs
opaque bytes.** It cannot see that the bytes are a token approval, a router call,
or a transfer of your entire balance to an attacker's address, so **it does not
stop draining.** A compromised host with `kms:Sign` can sign anything the wallet
could have signed. Treat KMS as "the key cannot be exfiltrated and reused later",
not as "the funds are safe while the box is owned".

Consider the remote-signer move the moment the wallet balance stops being an
amount you would shrug at — and consider the exchange-side controls *now*,
because the main key's blast radius does not scale with the wallet.

---

## 4. Operating it

The CLI is the only entrypoint. Exit codes: `0` ok / gate pass · `1` refused or
gate reject · `2` usage or config error.

```
memebuy_cli.py [--config PATH] [--verbose]
               [--dry-run | --status | --check-token CHAIN ADDRESS | --run-once]
               [--chain bsc|solana] [--token ADDR] [--amount USDT]
               [--i-understand-this-spends-real-money]
```

`--config` defaults to `data/config.json`. The `meme_buy` block is validated in
full or not at all: unknown keys are rejected outright, so a typo'd
`per_trade_usdt_capp` is a startup error instead of a silent fallback to a
default. Start from `config.meme_buy.example.json` — it is a **fragment** to be
merged into the real `config.example.json`, not a replacement for it, and the
`__comment__` lines must be stripped because the loader rejects unknown keys.

### `--dry-run` (default, no arguments)

```
$ python3 memebuy_cli.py
meme-buy CLI — dry-run (default). Nothing was moved.
  --status                      show config, arming state and caps
  --check-credentials           which Bitget key files each role has
  --check-token CHAIN ADDRESS   safety gate only (read-only)
  --run-once ...                requires armed config AND --i-understand-this-spends-real-money

the spot bot (autobuy.py) is untouched by this subsystem
```

### `--status`

Read-only. It will not even create the journal database as a side effect of
being asked a question.

```
$ python3 memebuy_cli.py --status --config data/config.json
meme-buy status
  config file .............. data/config.json
  config armed ............. False
  MEME_BUY_ARMED=1 in env .. False
  config dry_run ........... True
  EFFECTIVE MODE ........... DRY-RUN
  per-trade cap (USDT) ..... 25
  daily cap (USDT) ......... 100
  kill switch file ......... data/MEME_BUY_KILL
  kill switch engaged ...... False
  bitget SUB key prefix .... BITGET_SUB (stage 1 transfer)
  bitget MAIN key prefix ... BITGET_MAIN (stage 2 withdrawal)
      run --check-credentials to see which files are actually present
  chain bsc     ........... enabled
      withdraw address ..... 0x8fA3...c41B
      allowlisted tokens ... 1
      slippage (bps) ....... 100
      swap backend ......... native
  chain solana  ........... disabled
  journal db ............... data/meme_buy.sqlite3
  spent in last 24h (USDT) . 0

  the spot bot (autobuy.py) is not affected by any of the above
```

`EFFECTIVE MODE` is the only line that matters. It reads `LIVE` only when the
config is armed **and** `MEME_BUY_ARMED=1` is in the environment **and**
`dry_run` is false. A locked or corrupt journal degrades the spend line to
`unavailable (...)` rather than crashing the command.

### `--check-credentials`

Read-only, and **offline**: it makes zero network calls and never opens the
credential files, so no key, secret or passphrase can reach your terminal or a
CI log. It reports, per role, whether the three files are present and readable,
and states plainly what that role is allowed to do.

```
$ python3 memebuy_cli.py --check-credentials --config data/config.json
meme-buy credential check
  read-only: no network calls, no files opened, no secret values printed

  SUB-role key (prefix BITGET_SUB)
    BITGET_SUB_API_KEY_FILE            set -> /run/secrets/bitget_sub.key (readable)
    BITGET_SUB_API_SECRET_FILE         set -> /run/secrets/bitget_sub.secret (readable)
    BITGET_SUB_API_PASSPHRASE_FILE     UNSET (must hold the PATH of the secret file)
    status ......................... INCOMPLETE — missing/unreadable: BITGET_SUB_API_PASSPHRASE_FILE
    may ............................ transfer USDT from the sub-account up to the main account (stage 1)
    may NOT ........................ withdraw anything: Bitget never grants a sub-account key withdraw permission, ...
  ...
```

Exit code 0 when both roles are ready, 1 when anything is missing. Run it before
arming: a live run refuses to start without both key sets anyway, and finding
out here costs nothing.

### `--check-token CHAIN ADDRESS`

Runs `safety.gate.screen_token` and nothing else. Read-only HTTP GETs and
JSON-RPC reads; no exchange calls, no signing, spends nothing. Safe to run
against anything, as often as the upstream rate limits allow.

```
$ python3 memebuy_cli.py --check-token bsc 0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82
running safety gate only — read-only, spends nothing
  chain=bsc address=0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82

  [PASS] 01_allowlist            0x0E09...CE82 in bsc allowlist (1 entries)
  [PASS] 02_address_format       EIP-55 checksum valid
  [PASS] goplus_fetch            34 fields
  [PASS] 03_goplus_open_source   is_open_source=1
  [PASS] 04_goplus_honeypot_flags all three flags 0
  [PASS] 05_goplus_taxes         buy_tax=0.0000 sell_tax=0.0000 (fractions; max 0.1000/0.1000)
  [PASS] 06_goplus_mutable_flags no mutable flags set
  [PASS] 07_lp_locked_or_burned  lp locked/burned 98.41% (min 95.00%)
  [PASS] 13_holder_concentration top-10 non-pool holders 21.07% (max 30.00%)
  [PASS] 08_honeypot_is_simulation simulated sell ok, riskLevel 1
  [PASS] dexscreener_fetch       pair 0x7EFa...0f31 on pancakeswap
  [PASS] 14_liquidity            liquidity $4,120,883 (min $50,000)
  [PASS] 15_pair_age             pair age 41216.6h (min 24.0h)
  [PASS] 16_volume_and_flow      24h volume $612,004, m5 31b/24s
  [FAIL] 17_price_impact         price impact not supplied by the caller (DEX quote required)

  verdict: REJECT (15 checks): 17_price_impact: price impact not supplied by the caller (DEX quote required)
```

That trailing `FAIL` is expected from the CLI and is not a defect: check 17
needs a live DEX quote, which only the orchestrator has at stage 4/5. Read
`--check-token` as "everything except price impact", and read a non-17 failure
as a real rejection. Exit code is `1` whenever the verdict is not a pass.

### `--run-once`

Refuses unless **all three** of the following agree. They are independent on
purpose, and no two of them can be set by the same action:

1. `meme_buy.armed: true` in the config file,
2. `MEME_BUY_ARMED=1` in the process environment — **restart-only**, and
3. `--i-understand-this-spends-real-money` on the command line.

```
$ python3 memebuy_cli.py --run-once --chain bsc \
    --token 0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82 --amount 25 \
    --i-understand-this-spends-real-money
mode: LIVE
{
  "ok": true,
  "status": "completed",
  "stage": 7,
  "reason": null,
  "run_id": 14,
  "client_oid": "mb-bsc-9f2c1a0d7b4e6f81",
  "dry_run": false,
  "detail": {
    "transfer": {"transferId": "1213...041"},
    "withdraw": {"orderId": "1213...903"},
    "arrival_balance": 24.6,
    "arrival_delta": 24.6,
    "gate": {"passed": true, "checks": [...], "reasons": []},
    "swap": {"tx_hash": "0x1f6c...", "amount_in": 24600000000000000000, "status": "success"}
  }
}
```

Refusals are explicit and cost nothing:

```
$ python3 memebuy_cli.py --run-once --chain bsc --token 0x... --amount 25
REFUSING: --i-understand-this-spends-real-money was not passed.
  --run-once can move real funds; the acknowledgement flag is required.
$ echo $?
1
```

If the config is armed but the environment variable is missing, the run
*proceeds in dry-run* rather than refusing, and says so on stderr:

```
NOTE: MEME_BUY_ARMED=1 is not set in this process environment, so the
      orchestrator stays in dry-run regardless of the config flag.
mode: DRY-RUN
```

### Arming — an explicit human step

Arming is not something the software can do to itself, and not something a
config reload can do to a running process. `MemeBuyOrchestrator` snapshots the
armed decision **once**, at construction, and never re-reads it. Because half of
that decision lives in the process environment, arming requires a restart. This
is what stops the webapp — or anything else that can write the config file —
from arming a running bot.

```sh
# 1. put the token in BOTH allowlists
$EDITOR /etc/minas/allowlist.json            # SAFETY_ALLOWLIST_FILE
$EDITOR data/config.json                     # meme_buy.chains.<chain>.token_allowlist

# 2. flip the config flags
#      meme_buy.armed   -> true
#      meme_buy.dry_run -> false

# 3. confirm the config parses and the caps read the way you expect
python3 memebuy_cli.py --status              # EFFECTIVE MODE still DRY-RUN here

# 4. arm the process environment and restart
export MEME_BUY_ARMED=1
python3 memebuy_cli.py --status              # EFFECTIVE MODE ........... LIVE

# 5. one buy, with the acknowledgement
python3 memebuy_cli.py --run-once --chain bsc --token 0x... --amount 25 \
    --i-understand-this-spends-real-money
```

### Disarming

Disarming is instant and does not need a restart, because it is checked before
every stage rather than snapshotted:

```sh
touch data/MEME_BUY_KILL      # the kill switch. Stops the pipeline before every stage.
rm data/MEME_BUY_KILL         # release
```

`kill_switch_engaged()` returns **True (stop)** when the path is unset, empty,
of the wrong type, or the filesystem cannot be read. An unconfigured kill switch
means the operator has no way to halt the pipeline, which is itself a reason not
to run.

Slower, more permanent options: set `meme_buy.armed: false` (takes effect at the
next construction), or unset `MEME_BUY_ARMED` and restart (takes effect
immediately at restart). The kill switch is the one to reach for in a hurry.

### Reading the journal

SQLite, at `meme_buy.db_path` (default `data/meme_buy.sqlite3`) — deliberately
**not** the webapp's `registry.db`, so a webapp migration, wipe or restore can
never reset the rolling daily spend cap.

`meme_buy_runs` — one row per attempted buy, keyed by a `UNIQUE client_oid`.
`meme_buy_events` — two rows per stage, `begin` before the stage acts and `end`
after it returns, plus `fail` / `resume` / `complete`.

```sh
sqlite3 data/meme_buy.sqlite3 \
  "SELECT id, created_at_iso, dry_run, stage, status, chain, token, usdt_amount,
          withdraw_tx_hash, swap_tx_hash, error
     FROM meme_buy_runs ORDER BY id DESC LIMIT 10;"

# the full trace of one run
sqlite3 data/meme_buy.sqlite3 \
  "SELECT ts_iso, stage, phase, substr(detail,1,160)
     FROM meme_buy_events WHERE run_id = 14 ORDER BY id;"

# what the gate actually said, for a run that was rejected
sqlite3 data/meme_buy.sqlite3 \
  "SELECT gate_verdict FROM meme_buy_runs WHERE id = 14;"

# what the rolling cap currently sees (dry runs excluded)
sqlite3 data/meme_buy.sqlite3 \
  "SELECT COALESCE(SUM(usdt_amount),0) FROM meme_buy_runs
    WHERE dry_run = 0 AND committed_at IS NOT NULL
      AND committed_at >= strftime('%s','now') - 86400;"
```

Two accounting rules worth internalising:

* `committed_at` is stamped **before** the first money-moving request, not after
  it succeeds. A request that times out may still have been executed by the
  exchange, so it counts against the daily cap either way.
* Dry-run rows never count.

`status` values you will see: `pending`, `running`, `completed`, `failed`,
`aborted`, `killed`, `gate_rejected`, `arrival_timeout`, `resume_blocked`.
`resume_blocked` always means a human has to reconcile before anything else
happens — see §6.

---

## 5. The safety gate

`safety/gate.py` runs 17 numbered checks in cheap-first order and returns a
`GateVerdict`. It performs only read-only GETs and JSON-RPC reads, it never
moves money, and **it cannot raise**: every exception, timeout, HTTP 429,
non-200 response and *missing field* becomes a failed check. The orchestrator
treats `verdict.passed is not True` as a hard stop — including a verdict that
came back carrying an internal error.

Absence is never safety. GoPlus omits fields it cannot determine; verified live,
PancakeSwap's own CAKE returned no `cannot_sell_all`, no `buy_tax`, no
`sell_tax` and no `lp_holders`. A gate written `if data.get("is_honeypot") ==
"1": reject` fails **open** on exactly the thin, brand-new tokens this pipeline
looks at. Missing field => REJECT, reason `field X missing`.

A verdict may also only pass if every rule for that chain actually ran; a code
path that quietly skipped a check would otherwise read as a clean scan.
`_finalise` adds a synthetic `gate_incomplete` failure instead.

### The checks

| #  | Check | Chain | Threshold | Source |
| -- | ----- | ----- | --------- | ------ |
| 01 | `01_allowlist` | both | address must be present | local `SAFETY_ALLOWLIST_FILE` |
| 02 | `02_address_format` | both | EIP-55 checksummed EVM / base58 32-byte mint | local |
| 03 | `03_goplus_open_source` | bsc | `is_open_source == "1"` | GoPlus |
| 04 | `04_goplus_honeypot_flags` | bsc | `is_honeypot`, `cannot_sell_all`, `cannot_buy` all `"0"` | GoPlus |
| 05 | `05_goplus_taxes` | bsc | buy tax <= 0.10 and sell tax <= 0.10 (GoPlus taxes are **fractions**) | GoPlus |
| 06 | `06_goplus_mutable_flags` | bsc | all 10 of `is_proxy`, `slippage_modifiable`, `personal_slippage_modifiable`, `transfer_pausable`, `is_blacklisted`, `hidden_owner`, `can_take_back_ownership`, `selfdestruct`, `is_mintable`, `anti_whale_modifiable` literally `"0"` | GoPlus |
| 07 | `07_lp_locked_or_burned` | bsc | >= 95% of LP locked or burned | GoPlus `lp_holders` |
| 08 | `08_honeypot_is_simulation` | bsc | simulation succeeded, not a honeypot, `riskLevel < 12` | honeypot.is v2 |
| 09 | `09_solana_authorities` | solana | `mintAuthority` null **and** `freezeAuthority` null | Solana JSON-RPC `getAccountInfo` |
| 10 | `10_token2022_extensions` | solana | known token program; no dangerous extension (**unknown extension == dangerous**) | Solana JSON-RPC |
| 11 | `11_rugcheck_risk` | solana | no risk at level `danger`; `score_normalised <= 20` (**higher is worse**) | RugCheck |
| 12 | `12_rugcheck_lp_lock` | solana | `lpLockedPct >= 95`; every locker unlocks >= 30 days out; absent unlock date = reject | RugCheck |
| 13 | `13_holder_concentration` | both | top-10 non-pool holders < 30%, excluding LP vaults / lockers / burn | GoPlus `holders` |
| 14 | `14_liquidity` | both | >= $50,000 | DexScreener |
| 15 | `15_pair_age` | both | >= 24h, and `pairCreatedAt` must be a plausible ms timestamp | DexScreener |
| 16 | `16_volume_and_flow` | both | 24h volume >= $25k; volume/liquidity in [0.5, 10]; `txns.m5.sells >= 1` | DexScreener |
| 17 | `17_price_impact` | both | quoted impact <= 500 bps (5%) | the DEX quote, passed in by the caller |

Every threshold lives in the frozen `Thresholds` dataclass and can be overridden
per call.

Two direction traps are pinned by comments, constants *and* dedicated unit
tests, because inverting either one turns the gate into a scam detector that
only buys scams: RugCheck's `score_normalised` is **higher = worse**, and
GoPlus percentages are **0..1 fractions** (a value outside that range means the
units moved upstream, and the gate rejects rather than guessing).

Tag matching is whole-word, never substring. Substring matching reads "Unlocked
team wallet" as locked (it contains "lock") and drops a whale tagged "Alpaca
Finance" from the concentration sum (it contains "lp") — both flip a check from
REJECT to PASS, which is the one direction that costs money.

### LIMITATIONS — read this before trusting a green verdict

* **It cannot catch a slow rug.** Nothing here models a team quietly selling into
  the book over days, or LP that is locked today and unlocks next month. Unlock
  timers are parsed, but a lock that merely outlasts the threshold is still a
  countdown, not safety.
* **It cannot see insider or bundled wallets.** Supply split across 200 fresh
  addresses funded from one source passes the top-10 concentration check.
* **A clean scan is valid for roughly one block.** Everything is read before the
  buy and can change in the block the buy lands in — including by a sandwich, or
  by the owner front-running the bot's own transaction.
* **Post-buy tax / pause / blacklist.** If any mutable flag is set, the owner can
  raise the sell tax to 100%, pause transfers, or blacklist a specific address
  *after* the buy settles. Checks 05 and 06 describe the token's state now, not
  its permissions later.
* **Post-buy freeze on Solana.** A live `freezeAuthority` can freeze your token
  account after the purchase; the position becomes unsellable while still
  showing a price.
* **Simulators cannot see traps that have not fired.** honeypot.is buys and sells
  against current state; a trap gated on block height, holder count, or a
  whitelist simulates clean. This has a structural consequence: **the early buyer
  is the one whose loss creates the signal every other tool reads.** Being first
  through this gate is not an edge, it is the sample.
* **This gate is a public adversarial target.** GoPlus, honeypot.is, RugCheck and
  DexScreener are free and scriptable, so scammers pre-test contracts against
  them until they come back clean. A passing score partly selects for scams
  competent enough to pass.
* **The dominant loss mode is not scored here at all**: a fully sellable, fully
  legitimate token whose price simply goes to zero. Every check above is about
  whether you *can* sell. None is about whether it will be *worth* anything.

### The numbers are folklore, not measurement

$50k liquidity, 24h age, 95% LP locked, 30% top-10, 30-day unlock, riskLevel 12,
`score_normalised` 20 — these are community rules of thumb that were assembled
from public write-ups and forum consensus. **None of them was derived from a
measured outcome distribution.** They are defaults, not findings.

The journal is the instrument for fixing that. Every run records the full
verdict blob alongside the outcome, so after enough runs you can ask which
checks actually separated the losses from the rest and move the constants in
`Thresholds` accordingly. Until then, treat a pass as "did not trip any of a
list of known-bad patterns" and nothing more.

---

## 6. Failure modes and recovery

| Symptom | Likely cause | What to do |
| ------- | ------------ | ---------- |
| `REFUSING: meme_buy.armed is false` | config not armed | Intended. See §4 before changing it. |
| `REFUSING: --i-understand-this-spends-real-money was not passed` | acknowledgement flag missing | Intended. |
| `mode: DRY-RUN` when you expected LIVE | `MEME_BUY_ARMED=1` not in *this* process's environment | Export it and restart. It is restart-only by design. |
| `config ... has no 'meme_buy' block` | the block was never merged into the real config | Merge `config.meme_buy.example.json`, strip `__comment__` keys. |
| `invalid meme_buy config: ... unknown key(s)` | typo, or a `__comment__` line left in | The loader rejects unknown keys on purpose. Fix the spelling. |
| `token_allowlist is empty while armed=true` | armed with nothing to buy | v1 has no discovery. Add the contract address. |
| `kill switch engaged (...) before stage N` | the kill-switch file exists — or its path is unset/unreadable | `rm` the file, or fix the path. An unreadable path reports as engaged. |
| Bitget **40037** `Apikey does not exist` | credentials in `config.json` not using `${VAR}` substitution, or the key was deleted | Use `${SUB1_API_KEY}` syntax; a bare name is sent literally. |
| Bitget **40038** on withdrawal only | the API key's IP allowlist no longer matches the box's public IP — usually an instance stop/start on a non-elastic IP | Allocate an Elastic IP, associate it, update the Bitget key allowlist. This fails *silently* in the sense that spot trading keeps working. |
| Bitget **40052** | a security setting (password / 2FA / email / phone) changed within the last 24h — withdrawals frozen | Wait out the window. Nothing to fix in code. |
| Bitget **43118** duplicate `clientOid` | the same oid was already accepted; usually a retry after a timeout | **Never a retry signal.** For a transfer the orchestrator treats it as "already sent". For a withdrawal it looks the status up. Do not resend by hand. |
| Withdrawal rejected, address not recognised | destination not in the Bitget withdrawal address book | Add it by hand in the Bitget UI. There is no API for this. |
| Withdrawal rejected from a sub-account key | sub-account keys cannot withdraw | Use a main-account key; stage 1 exists to move funds sub -> main first. |
| `funds did not arrive at ... within 900s` (`arrival_timeout`) | slow chain, exchange review, or the withdrawal never left | Check the withdrawal on Bitget and on-chain. Do **not** re-run the same oid until you know which. |
| `no destination balance probe available for <chain>` | `chains.<chain>_swap.get_usdt_balance` missing | Arrival cannot be proven and the run stops. The exchange's own "success" is deliberately not accepted as proof the funds are spendable. |
| BSC `BNB_FLOOR` | wallet has USDT but no BNB | Fund with >= 0.01 BNB. Gas is paid in BNB; the approve cannot even be sent. |
| BSC revert `Pancake: K` | a fee-on-transfer token hit a non-supporting swap function | Should not occur — the module always uses `swapExactTokensForTokensSupportingFeeOnTransferTokens`. If you see it, something changed the call path. |
| BSC revert `PancakeRouter: INSUFFICIENT_OUTPUT_AMOUNT` | `amountOutMin` too tight, almost always an unbudgeted buy tax (`getAmountsOut` models the 0.25% LP fee only) | Set `buy_tax_bps`, or raise `slippage_bps`. Note the fat-finger cap applies to the **sum**. |
| BSC revert `TransferHelper: TRANSFER_FROM_FAILED` | the approve is not mined yet, or the amount was computed with the wrong decimals (**BSC USDT is 18, not 6**) | Wait for the approve receipt; re-check decimals. |
| BSC revert `EXPIRED` | the 120s router deadline elapsed between signing and inclusion | Re-quote and resubmit. Nothing was spent. |
| `PREFLIGHT_REVERT` | `estimate_gas` says the swap would revert | Aborted before broadcast. Nothing was spent. Read the reason string. |
| `BROADCAST_UNCONFIRMED` / `RECEIPT_UNCONFIRMED` | the tx may already be in flight | **Do not retry.** `exc.nonce` / `exc.tx_hash` are set — reconcile on chain first. |
| Solana **6001** `SlippageToleranceExceeded (0x1771)` | price moved past `slippageBps` | Re-quote. **Never resubmit the signed transaction.** |
| Solana **6014** `IncorrectTokenProgramID (0x177e)` | the mint belongs to a different SPL token program (Token-2022) | Gate check 10 should have caught this. Do not force it. |
| Solana **6024** `InsufficientFunds (0x1788)` | balance does not cover amount + fees + ATA rent (2,039,280 lamports for a first-ever buy) | Fund with 0.03–0.05 SOL, not 0.01. |
| Solana `SOL_FLOOR` | below the configured/clamped SOL floor before quoting | Same fix. The floor is clamped upward if the config asks for less than ATA rent + a base fee. |
| Solana stale blockhash | the quote aged out (~150 slots) | The module re-quotes up to twice, then raises `STALE_GIVEUP`. A stale signed transaction is never resubmitted. |
| Solana `NO_SIMULATION` / `simulationError` set | Jupiter could not simulate | Aborted before signing. A "just send it" retry falls back to the 1,400,000 CU max and inflates the fee as well as being wrong. |
| `resume_blocked: transfer ... was already submitted` | a transfer went out and the response was never seen; there is no per-oid transfer lookup | Reconcile the Bitget balances by hand, then clear or complete the run row. |
| `resume_blocked: withdrawal ... is 'pending' on resume` | the withdrawal is not confirmed successful | Do not resend. Wait or reconcile with Bitget. |
| `resume_blocked: a swap was already attempted` | a swap may be on the wire or in a block, and there is no per-oid swap lookup | Reconcile the wallet on chain, then clear or complete the run. A repeat is a second real trade. |
| `resume_blocked: ... refusing to resume across modes` | the row is a live run and this process is dry-run (or vice versa) | Arming is restart-only; a run spanning two modes is an operator reconciliation, not something to guess at. |
| `safety gate REJECT: [...]` | one or more checks failed | Read the reasons list in `gate_verdict`. If the withdrawal already happened, the USDT is sitting in your wallet — that is by design, see §7. |
| `safety gate unavailable: ...` | provider down, 429, or a missing module | Treated as REJECT. Fail-closed is not a bug. |
| `does not accept 'dry_run'; refusing to call it` | a `chains/` swap signature drifted | Hard abort. A `**kwargs` tail is not evidence that `dry_run` is honoured, so the safety-critical names must be real named parameters. |

---

## 7. Irreversibility

The pipeline crosses exactly one point of no return, and it crosses it *before*
the safety gate runs. That ordering is deliberate: the gate needs a live DEX
quote from the destination chain, which does not exist until the funds are
there. The consequence is that a gate rejection can leave USDT sitting in your
on-chain wallet. That is the intended failure — the money is yours, unspent, and
recoverable by hand.

| Stage | Action | Undoable? | How, and what it costs |
| ----- | ------ | --------- | ---------------------- |
| 0 | select / guardrails | n/a | Nothing has moved. |
| 1 | sub -> main USDT transfer | **Yes** | Transfer it back main -> sub in the Bitget UI. Internal, free, immediate. |
| 2 | **on-chain withdrawal** | **Effectively no** | Once submitted it cannot be cancelled. Small automated withdrawals skip manual review entirely, so there is no window in which support can stop it. If it lands at the right address you still hold the USDT and can send it back manually, paying network fees; if the address is wrong the funds are gone. |
| 3 | await arrival | n/a | A read-only poll. Timing out costs nothing beyond the already-completed stage 2. |
| 4 | safety gate | n/a | Read-only. A reject here stops the run with the USDT in your wallet. |
| 5/6 | **swap** | **Never** | A broadcast swap cannot be un-sent. Confirmed or reverted, the outcome is final; a revert still burns gas. Selling back is a new trade at a new price, not an undo. |
| 7 | journal outcome | n/a | Bookkeeping only. |

Practical rules that follow from the table:

* Everything cheap and reversible is checked **before** stage 2: caps, both
  allowlists, address format, the destination allowlist, the kill switch.
* The `client_oid` is written to disk **before** each money-moving request, and
  the amount counts against the daily cap from that moment — not from a
  successful response. A timeout is not evidence that nothing happened.
* On resume the orchestrator never resends blindly. A withdrawal is looked up by
  oid; a transfer or a swap that may already be in flight is a `resume_blocked`
  abort for a human, because neither has a per-oid lookup that could distinguish
  "not sent" from "sent, response lost".
* The swap is sized by what **arrived**, never by what was requested. The
  exchange deducts its withdrawal fee from the amount sent, so asking the router
  for the full requested amount either reverts (gas burnt) or quietly spends
  unrelated USDT that happens to be in the wallet.
