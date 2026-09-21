# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.4] — 2026-09-21: holdings are net and located; prices follow the pair; analytics states facts

Corrections from the first live XRP and XLM buys on the production server.

### Changed

* **Holdings are net of the trading fee charged in the coin.** The gross fill
  stays on the transaction row (`amount_crypto`, what Bitget's order history
  shows) with `net_amount` beside it; the tax CSV gains `net_quantity`.
  Holdings, average buy price, P&L and cost shares use the net figure.
  Proved against the live account on 2026-09-21: BTC 0.000197 bought, fee
  1.97e-07, Bitget balance 0.000196803; XLM 14.32 − 0.01432 = 14.30568; XRP
  1.3334 − 0.0013334 = 1.3320666, each to the last digit.
* **Holdings say where the coins are.** `total_amount` = acquired,
  `on_exchange` = acquired minus what each real withdrawal took off the
  Bitget balance, `sent_to_wallet` = what those withdrawals delivered. The
  mode each withdrawal was sent under is read from the row (exclusive:
  `expected_received == amount_executed`, balance drops by size + fee;
  inclusive: `expected_received == amount_executed − fee`, balance drops by
  size), never from today's configuration. Simulated withdrawals move nothing.
* **Prices follow the pair's precision** (`price_precision`: BTC 2, XRP 4,
  XLM 5) on transaction rows, holdings and analytics, and in emails, so XLM's
  0.20945 fill no longer shows as 0.21.
* **Analytics copy states the metric only** (reviewed for compliance):
  volatility entries carry `summary` ("30-day volatility 3.1% (moderate).")
  instead of `recommendation`; best/worst performer dropped; the
  diversification score replaced by `share_of_invested` (share of COST,
  labelled as such). The "Estimates only, not financial advice" note stays.
  SDKs regenerated (breaking rename).
* Runbook: after the first real withdrawal, compare the app's `on_exchange`
  figure with Bitget's balance to the last digit and **record** the result;
  that comparison is what confirms `BITGET_WITHDRAW_FEE_MODE`.

### Added (0.6.5, 2026-09-21)

* **Migration 025** — `purchase_transactions.withdrawal_fee_mode` with CHECK
  `ck_purchase_transactions_withdrawal_fee_mode` (`exclusive` / `inclusive` /
  NULL). The executor writes the configured mode on every withdrawal row at
  send time; readers prefer the column and fall back to the
  `expected_received` derivation only for rows written before 025. A test
  asserts every executor-written row carries a non-NULL mode.

### Known issues

* `tests/api/test_admin_alert_digest.py::test_nothing_is_emailed_while_no_alert_is_critical`
  is order-dependent: it passes alone and fails in the full `tests/api` run
  when an earlier test leaves a stopped purchase or a recent failed
  transaction behind, because the alert computation is platform-wide, not
  per user. Tracked as GitHub issue #35; not a product defect. Do not re-run
  CI to green over it — fix the test's isolation.

## [Unreleased] — AutoBuy platform, 2026-09-16 / 2026-09-17

Platform releases are tagged separately on GitHub (v0.5.0 covers everything up
to commit bd93adf); this entry records what shipped after it.

### Added

* **XRP and XLM as purchase coins**, each on its single native chain (Bitget
  chain names `XRP` and `StellarLumens`). Address validation for the XRP
  Ledger (classic `r…` Base58Check and `X…` X-addresses, tag decoded) and
  Stellar (`G…` StrKey with CRC16 and `M…` muxed addresses, memo id decoded),
  server side and in the web form. Checksums verified against generated
  addresses and known funded accounts.
* **Migration 023** — `wallets.destination_type` / `wallets.memo`,
  `purchases.wallet_destination_type` / `purchases.wallet_memo`,
  `purchase_transactions.withdrawal_memo`, all nullable, with CHECK
  constraints `ck_wallets_destination_type` and
  `ck_purchases_wallet_destination_type` restricting the type to
  `self_custody` / `exchange` / NULL. Applied locally 2026-09-17 (alembic
  `023`, both constraints present); **not yet applied on the Tokyo server**
  (planned as a deliberate step with a `pg_dump` first, see
  `docs/DEPLOYMENT.md`, "Go-live runbook", Step 0).
* **The destination-type rule** (`api/utils/chains.check_destination`), one
  function for purchase create, wallet change, memo edit, switch to
  withdraw-immediately, retry-withdrawal, `POST /api/wallets` and the
  executor. On a chain Bitget reports as needing a tag: refuse when the
  destination type is unset; refuse `exchange` without a memo; refuse a memo
  on `self_custody`; an X-address / muxed address supplies its own tag and a
  conflicting typed memo is refused; the base address is what gets stored. On
  any other chain a memo or destination type is refused. The executor applies
  the same rule at send time (`TAG_REQUIRED`): the withdrawal row fails, the
  purchase parks as `withdrawal_failed` with the reason, no re-buy.
* `GET /api/v2/market/destination` — the same verdict before a form submits,
  plus a public-ledger existence probe (XRPL `account_info`, Horizon). A
  never-funded address is a warning, never a block. `tag_label` on the market
  chains (`"destination tag"` / `"memo"`).
* Web form: the required "Where is this XRP address?" question with the
  approved wording; X/M addresses auto-select *exchange* with the decoded tag
  read-only; the memo is shown on purchases and withdrawal rows.

### Verified

* **Eleven curl proofs against the API, bypassing the UI** (2026-09-16, live
  Bitget chain data, live XRPL node): exchange without tag → 422
  `memo_required`; destination never answered → 422
  `destination_type_required`; X-address with a conflicting typed tag → 422
  `memo_conflict`; self-custody with a tag → 422 `memo_not_expected`; BTC with
  a memo → 422 `memo_not_used`; self-custody → 201; exchange with tag → 201
  (memo stored); `POST /api/wallets` exchange without tag → 422; X-address
  saved as classic address + tag → 201; destination check reports the
  unfunded warning; `GET /api/v2/market/coins/XRP` reports `need_tag` true.
* **Two executor proofs in the real local container** (2026-09-17, dry run,
  live Bitget chain data, real `ExchangeClientGateway`): an XRP purchase
  written straight into the database with no destination type, and one with
  `exchange` and no memo, both bought (simulated) and then **refused at send
  time** — withdrawal row `failed`, purchase `withdrawal_failed`, reason
  stored, nothing sent.
* **Live fee:** the first real BTC buy on the Tokyo server (2026-09-16 20:44
  UTC, 0.000197 BTC) paid 1.97e-07 BTC, i.e. **0.1%**, while Bitget's public
  symbols endpoint quotes a 0.2% taker rate. The UI estimate therefore
  overstates the fee for this account; the 3% fill-warning band is
  unaffected.

### Not verified

* Bitget's withdrawal request field `tag` (docs and SDK only). It is only
  exercised by a withdrawal to an exchange address with a tag attached, which
  is the first real XRP test in the go-live runbook.

### Excluded

* **KORU** — requested alongside XRP/XLM; not added. Bitget lists coin id
  2880 with an empty `chains` array and no `KORUUSDT` spot pair (error
  40034). It is a TradFi perpetual on a 3× leveraged Direxion ETF: no chain,
  no spot market, nothing to deliver to a wallet.

## [2.2.0] - 2026-07-21

Adds a second, opt-in swap backend and makes the Bitget credential model
explicit. **The spot bot is still untouched** and the meme-buy subsystem is
still manually invoked only.

### Added

* `chains/bitget_swap.py` — swap execution via the Bitget Wallet Trading API
  ("SwapX"), selectable per chain with
  `meme_buy.chains.<chain>.swap_backend: "bitget"`. Signature-compatible with
  `bsc_swap` / `solana_swap`, so the orchestrator can swap one back-end for
  another. Supports SwapX "NoGas" (`gasless: true`), which removes the need to
  keep a BNB/SOL float — it requires a stablecoin input and an order of roughly
  \$5 minimum.
  * **PROVISIONAL: this module has never been run against the live API.** It is
    written to the published documentation only, so every endpoint path, field
    name, envelope shape and signing detail in it must be confirmed on first
    use. Results carry `unverified_integration: true`. Everything uncertain is
    isolated so it can be corrected in one place: `PATHS`, `CHAIN_INDEX`, the
    `WIRE` alias dict, and the entire HMAC recipe in
    `BitgetSwapClient._auth_headers` — **that one method is what to correct if
    authentication fails.**
  * Fail-closed throughout: non-JSON bodies, absent/non-object `data`, and
    business-code failures inside an HTTP 200 all raise. Errors carry
    `exc.signed` — `False` means no signature existed and a retry is safe;
    `BitgetSwapUnconfirmed` means reconcile `exc.order_id` before any retry.
  * The order's minimum-received is checked against the amount the quote
    authorised and the swap is refused **before signing** if it protects less.
* `memebuy/credentials.py` and `memebuy_cli.py --check-credentials` — a
  read-only, **offline** presence check for the two Bitget API key sets. It
  never opens the credential files and makes no network calls, so no secret can
  reach a terminal or a CI log; it reports per role which of the three
  `*_FILE` variables are set and readable, and states plainly what that role
  may and may not do. A live run also verifies both key sets at stage 0, so a
  missing main key can no longer be discovered *between* the transfer and the
  withdrawal.
* `meme_buy.chains.<chain>.swap_backend` and `gasless` config keys, plus
  `meme_buy.bitget_sub_prefix` / `bitget_main_prefix` for the two key sets.
  `swap_backend` is validated even for a disabled chain, so a typo surfaces
  before somebody flips `enabled: true`.

### Changed

* `docs/MEME_BUY.md`:
  * new swap-backend comparison table (chains, API-key requirement and how to
    get one, gas requirements, tax-token handling) leading with the fact that
    **both backends still sign with a private key held on the server** — SwapX
    is a route provider, not a custody model, so switching backends does not
    reduce key-compromise risk. The one real operational advantage is gasless
    swaps removing the BNB/SOL float.
  * a prominent PROVISIONAL warning for the SwapX backend, naming
    `_auth_headers` as the single method to correct if auth fails.
  * a new note on what Bitget "Onchain" is **not**: the app's Onchain tab is
    UI-only with no public API, the exchange API exposes only
    Read/Trade/Transfer/Withdraw and has no onchain/DEX/Web3 section, and no
    major exchange offers that flow through a documented API — so the
    withdraw-then-swap pipeline is unavoidable.
  * the Bitget prerequisites are now a precise per-key checklist: SUB key =
    Transfer only (and a sub key structurally cannot withdraw whatever is
    ticked), MAIN key = Withdraw + Read, IP-allowlisted, with the EC2 public IP
    `18.179.57.78` called out as **not** elastic (a stop/start breaks
    withdrawals with error 40038), the manual-only address book, the 24h
    security-change freeze (error 40052), and main-account KYC + 2FA.
  * a blast-radius section: a withdraw-capable main key means a compromised host
    can drain the exchange balance, not just the hot wallet. Exchange-side
    address whitelisting plus a withdrawal cap is the main mitigation; moving
    signing to a policy-enforcing remote signer (Turnkey / Coinbase CDP server
    wallets — self-serve, EVM+Solana, spend caps and router allowlists enforced
    at signing) is the upgrade path that survives a compromised host. AWS KMS
    stops key theft but **not** draining, because it signs opaque bytes.

### Safety posture (unchanged, restated)

* `gasless: true` with `swap_backend: "native"` is a hard config error, not an
  ignored key — silently dropping it would leave an operator believing a wallet
  needs no BNB/SOL, and the swap would fail *after* the irreversible withdrawal.
* An injected SwapX client's `gasless` setting can only be turned on by the
  keyword, never quietly off, so the \$5 gate cannot be bypassed and the signer
  can never produce EIP-712 typed data for an order built in normal mode.
* Whichever backend is configured, the orchestrator still refuses to call a
  `buy_token` that does not take `dry_run` as a real named parameter.
* Tokens are identified by address/mint only, on both backends. Symbols are
  attacker-controlled metadata and are never resolved.

## [2.1.0] - 2026-07-20

New, opt-in subsystem that withdraws USDT from Bitget to a wallet you control
and swaps it for a pre-allowlisted token on BSC or Solana. **The spot bot is
untouched**: `autobuy.py` does not import it, does not call it, and has no hook
into it. The only entrypoint is `memebuy_cli.py`, run by hand.

### Added

* `memebuy/` — the stage machine and the guardrails.
  * `orchestrator.py`: 8-stage pipeline (select → sub→main transfer → on-chain
    withdrawal → arrival poll → safety gate → swap → outcome), kill switch
    checked before every stage, before/after journaling, idempotent resume by
    `client_oid`, and a `run_once()` that never raises.
  * `config.py`: strict, total validation of the `meme_buy` block — unknown keys
    are rejected so a typo cannot silently fall back to a default.
  * `caps.py`: pure fail-closed predicates — per-trade cap, rolling 24h cap,
    address allowlist, token allowlist, kill switch.
  * `state.py`: durable SQLite journal in its own database file (not the
    webapp's `registry.db`), so a webapp migration or restore can never reset
    the daily spend cap. Spend counts from *before* the first money-moving
    request, because a timed-out request may still have executed.
* `chains/` — on-chain execution back-ends.
  * `bsc_client.py` / `bsc_swap.py`: PancakeSwap V2 via
    `swapExactTokensForTokensSupportingFeeOnTransferTokens` (always — plain
    `swapExactTokensForTokens` reverts `Pancake: K` on any taxed token), exact
    per-trade approve (never infinite), BNB gas floor, `estimate_gas` pre-flight
    that aborts rather than broadcasting a known-failing tx.
  * `solana_client.py` / `solana_swap.py`: Jupiter Swap API V2
    (order → sign → execute), `simulationError` checked before signing, bounded
    re-quoting on a stale blockhash, and a SOL floor that accounts for the
    0.00203928 SOL of ATA rent on a first-ever buy.
  * `wallet_keys.py`: the single key loader for every chain. Env vars hold a
    *path*, never key material; keys are wrapped in a `SecretKey` whose repr,
    str and format all render `<redacted>`.
  * `errors.py`: typed, chain-labelled error family, including the two codes
    that are never safe to retry (`BROADCAST_UNCONFIRMED`, `RECEIPT_UNCONFIRMED`).
* `safety/` — the fail-closed pre-trade screen.
  * `gate.py`: 17 numbered checks in cheap-first order. Read-only, cannot raise;
    every exception, timeout, 429, non-200 and **missing field** becomes a
    failed check. A verdict passes only if every rule for the chain actually ran.
  * Clients for GoPlus, honeypot.is, RugCheck, DexScreener and the Solana mint
    account, each documenting the units and direction of the fields it returns.
* `memebuy_cli.py` — `--status`, `--check-token`, `--run-once`, dry-run default.
* `config.meme_buy.example.json` — inert config fragment (armed=false,
  dry_run=true, both chains disabled, both allowlists empty).
* `safety/allowlist.example.json` — empty token allowlist template.
* `docs/MEME_BUY.md` — full operator documentation: prerequisite checklist, key
  handling, CLI reference, gate limitations, failure modes, irreversibility.
* Tests for the swap paths, the gate and the orchestrator.

### Safety posture

* **No discovery.** v1 buys only tokens explicitly allowlisted by contract
  address. `armed: true` with an empty `token_allowlist` is a hard config error.
* **Three independent inputs to go live**, none of which can be set by the same
  action: `meme_buy.armed: true` in the config, `MEME_BUY_ARMED=1` in the
  process environment (restart-only), and
  `--i-understand-this-spends-real-money` on the CLI. A config hot-reload — or
  anything else that can write the config file, including the webapp — therefore
  cannot arm a running bot.
* **Instant disarm.** `touch data/MEME_BUY_KILL` stops the pipeline before every
  stage. An unset or unreadable kill-switch path reads as *engaged*.
* **Caps enforced before the first money-moving call** and persisted across
  restarts.
* **Withdrawal destinations are config-only**, asserted again immediately before
  the withdrawal call.
* **Resume never resends blindly.** A withdrawal is looked up by `client_oid`; a
  transfer or swap that may already be in flight becomes a `resume_blocked`
  abort for a human.
* **The swap is sized by what arrived**, not by what was requested — the
  exchange deducts its withdrawal fee from the amount sent.

### Known limitations

* The safety gate cannot detect slow rugs, insider/bundled wallets, or traps
  that have not fired; a clean scan is valid for roughly one block; and the
  dominant loss mode — a fully sellable token that simply goes to zero — is not
  scored by anything in it. See the LIMITATIONS section of `docs/MEME_BUY.md`.
* The gate's numeric thresholds are community folklore, not measured values.
  They should be calibrated against the journal.
* The safety gate runs *after* the irreversible withdrawal, because it needs a
  live DEX quote from the destination chain. A rejection therefore leaves USDT
  in the destination wallet, unspent.

## [2.0.0] - 2026-07-06

### Added
- **Multi-exchange support**: the bot can now trade on **Bybit**, **OKX**, and
  **Binance** in addition to Bitget. Each sub-account entry picks its venue
  independently.
- Per-venue REST clients (`bybit_client.py`, `okx_client.py`,
  `binance_client.py`) exposing the same normalized interface as the Bitget
  client: server time, symbol metadata (`status` / `minTradeUSDT` /
  `quotePrecision`), ticker price, available spot balance, quote-denominated
  market buy, and normalized order status.
- Shared `ExchangeAPIError` base exception (`exchange_errors.py`) so callers
  catch one error type regardless of venue; message format generalizes the
  historical `"Bitget error {code}: {message}"` style per exchange.
- `exchange` field in `config.json` sub-account entries (`bitget`, `bybit`,
  `okx`, `binance`).
- Web UI: the Exchange dropdown now offers all four venues; env-var hints
  omit the passphrase variable for Bybit/Binance (which have none).
- `--selftest` now checks connectivity, market data, and clock skew against
  **all four** exchanges; it passes only when every venue is reachable.
- Full unit-test coverage per client (`tests/test_bybit_client.py`,
  `tests/test_okx_client.py`, `tests/test_binance_client.py`) — signing,
  normalization, error mapping, and fallback paths, all with mocked HTTP.
- **Premium web-UI redesign**: dark Minas-Guardian visual style across all
  templates (login, client list, add/edit form).

### Changed
- README, deploy guides, and selftest output updated from Bitget-only wording
  to multi-exchange.
- `api_passphrase` is now required only for Bitget and OKX; Bybit and Binance
  entries need only key + secret.

### Compatibility
- Legacy Bitget-only configs keep working unchanged: `exchange` defaults to
  `bitget` when absent, in both the bot config and the web-UI registry.

## [1.x]

Prior Bitget-only functionality:

- Bitget sub-account USDT auto-buy bot: polls each sub-account's spot USDT
  balance and market-buys its configured coin when the threshold is reached,
  with reserve, per-order cap, and precision flooring.
- Unit tests (mocked Bitget API), `${VAR}` env-var secret expansion in
  `config.json`, and Alibaba Cloud deployment assets (ECS walkthrough,
  Docker + systemd paths).
- One-shot server bootstrap script (`deploy/bootstrap.sh`).
- `--selftest` mode: validate server readiness (connectivity, clock skew)
  without API keys.
- Tailscale-ready admin web UI (client registry with apply-to-bot) and bot
  config hot-reload, plus the AWS EC2 deployment guide.
