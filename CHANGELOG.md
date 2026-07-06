# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
