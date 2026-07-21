# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
