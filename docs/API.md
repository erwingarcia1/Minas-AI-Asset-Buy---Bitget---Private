# AutoBuy Crypto API — Reference

Automated cryptocurrency purchasing: users schedule recurring USDT buys
(one-time / daily / weekly / monthly) that the platform executes on Bitget,
gated by KYC verification and verified, encrypted API keys.

Interactive docs: **`/docs`** (Swagger UI) · **`/redoc`** (ReDoc) ·
machine-readable spec at **`/openapi.json`**.

## Getting started (curl walkthrough)

```bash
BASE=http://localhost:8000

# 1. Register (password: ≥8 chars, upper+lower+digit+special)
curl -s $BASE/api/auth/register -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"Str0ng!passw0rd"}'

# 2. Login → token pair
TOKENS=$(curl -s $BASE/api/auth/login -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"Str0ng!passw0rd"}')
ACCESS=$(echo $TOKENS | jq -r .access_token)
AUTH="Authorization: Bearer $ACCESS"

# 3. Verify identity (returns a Sumsub WebSDK token for the frontend widget)
curl -s -X POST $BASE/api/kyc/initiate -H "$AUTH"

# 4. Store + verify a Bitget API key (spot-trade only; no withdrawal permission)
KEY=$(curl -s $BASE/api/exchange/keys -H "$AUTH" -H 'content-type: application/json' \
  -d '{"exchange":"bitget","api_key":"<key>","api_secret":"<secret>"}')
curl -s -X POST $BASE/api/exchange/keys/$(echo $KEY | jq -r .id)/verify -H "$AUTH"

# 5. Buy $100 of BTC weekly and send it to your wallet (Auto-Buy)
curl -s $BASE/api/v2/purchases -H "$AUTH" -H 'content-type: application/json' \
  -d '{"crypto_symbol":"BTC","amount_usd":100,"frequency":"weekly","purchase_type":"autobuy",
       "wallet_address":"bc1q...","blockchain_network":"BTC","exchange_account_id":"'$(echo $KEY | jq -r .id)'"}'

# 6. Watch it happen
curl -s $BASE/api/transactions/ -H "$AUTH"
curl -s $BASE/api/dashboard/ -H "$AUTH"
```

## Authentication

JWT bearer tokens. `POST /api/auth/login` answers `{access_token,
token_type, expires_in, csrf_token}` and sets two cookies scoped to
`/api/auth` with `SameSite=Lax` (and `Secure` outside development):
`autobuy_refresh` (the 7-day revocable **refresh token**, `HttpOnly`, path
`/api/auth`, never in a response body) and `autobuy_csrf` (readable, path
`/` so the page can read it on any route). The 30-minute **access token**
must be kept in memory only. The signing key is `SECRET_KEY` from the
environment, so sessions survive API restarts; a refresh fails only for a
revoked, expired or pre-password-change token (each rejection is logged
with its reason, and every CSRF failure logs `csrf_failed` with the reason).

- Send `Authorization: Bearer <access_token>` on every protected call.
- On 401 or page load, `POST /api/auth/refresh` with no body and the header
  `X-CSRF-Token: <csrf cookie value>`; the browser sends the refresh cookie.
  Both cookies are rotated and the previous refresh token is revoked.
  401 = no valid cookie (sign in again), 403 `csrf_failed` = header missing
  or mismatched.
- `POST /api/auth/logout` (same CSRF header) revokes the refresh token
  server-side and clears both cookies. Always 204.
- `POST /api/auth/change-password` invalidates **every** token issued before it.
- Refresh tokens are rejected as access tokens and vice versa; a refresh
  token posted in a body is ignored.
- Same-origin only: the Vite dev server proxies `/api` to the API and nginx
  does the same in production, so the cookies are first-party in both.
  Non-browser SDK clients need a cookie jar (the Python client's httpx has
  one; plain Node `fetch` does not and must log in again instead).

## Endpoints by module

| Module | Endpoints |
|---|---|
| auth (`/api/auth`) | `POST /register` · `/login` · `/verify` · `/refresh` · `/logout` · `/change-password` · `/forgot-password` (emails a 1-hour single-use link; always `{sent: true}`) · `/reset-password/validate` (`{token}` → 200 `{valid: true}` / 400 `{valid: false}`, does not consume the token) · `/reset-password` (`{token, new_password}` → 204; signs out every other session) |
| user (`/api/user`) | `GET/PUT /profile` · `GET /{user_id}` |
| kyc (`/api/kyc`) | `POST /initiate` · `GET /status` · `POST /create-applicant` · `GET /applicant/{id}/status` · `POST /webhook` (Sumsub, HMAC) |
| auto-buy (`/api/v2/purchases`) | `POST /` (autobuy: wallet required; dca/accumulate: wallet optional; `blockchain_network` is the exchange's chain name (`BTC`, `ERC20`, `TRC20`, `SOL`, `ArbitrumOne`, …; defaults to the coin's native chain); any wallet is validated against that chain's address format and rejected if it is a known burn/unspendable address; on Bitget the pair, minimum order, chain and (for autobuy) the minimum withdrawal are checked live, see "Bitget market data") · `GET /` (with latest transaction) · `GET /{id}` (full history) · `GET /{id}/transactions?status_filter=` · `PUT /{id}` (`status: paused|active`, amount, frequency, `travel_rule_info`, or `purchase_type` switch — switching to `autobuy` requires `wallet_address`, `blockchain_network` and `confirm_wallet_address`) · `POST /{id}/retry-withdrawal` (withdrawal_failed / needs_info only: re-sends the last filled buy's coins, never buys again) · `DELETE /{id}` (cancel, history kept) |
| wallets (`/api/wallets`) | `POST /` (`blockchain_network` = exchange chain name; the address format must match the chain family: bitcoin mainnet only, EVM `0x…`, Solana base58, Tron `T…`, XRP Ledger `r…`/`X…`, Stellar `G…`/`M…`; tag chains also take `destination_type` + `memo`, see "Destination tag / memo") · `GET /?blockchain_network=` · `DELETE /{id}` |
| notifications (`/api/notifications`) | `GET/PUT /preferences` — channel toggles, quiet hours, and the optional email categories `email_purchase_confirmations`, `email_weekly_summary`, `email_milestones` (stored per user in `notification_preferences`); `always_on_emails` lists what can never be switched off: password reset, password changed, purchase failed, withdrawal failed, withdrawal needs information. The weekly summary footer links to `/settings/notifications` (built with `build_app_link`) · `GET /history` · `POST /test` |
| email (`/api/email`) | `POST /send-verification` · `POST /send-notification` (own address only; admins may notify anyone) |
| webhooks (`/api/webhooks`) | `POST /sumsub` (HMAC via `x-payload-digest` / `x-payload-digest-alg`) · `POST /resend` |
| market (`/api/v2/market`) | `GET /coins/{coin}?amount_usd=` — the coin's Bitget USDT pair (online, minimum order, taker fee) and every withdrawal chain with fee, minimum, precision, address family and, when `amount_usd` is given, the fee in USD, the estimated coins received and whether the amount clears the minimum withdrawal (`meets_minimum`) |
| exchange (`/api/exchange`) | `GET /supported` (exchanges this deployment accepts keys for, from `SUPPORTED_EXCHANGES`, default `bitget`; other exchanges are rejected with 422 `exchange_not_supported`) · `POST/GET /keys` · `DELETE /keys/{id}` · `POST /keys/{id}/verify` — one harmless authenticated call (Bitget account info); the key records `verification_status` (pending \| verified \| failed), a readable `verification_error` (never credentials), `verified_at`, `last_checked_at`, `withdrawal_enabled` (warn the user; the executor still requires `verified`), the reported `permissions` and `extra_permissions`. Bitget codes confirmed from its "Edit API key" screen: `stow` Spot → Trade (required; missing → `no_trade_permission` "Spot trading permission is required"), `smow` Margin → Trade, `taxw` Taxation → Search, `p2pw` P2P → Search, `wtow` Wallet → Transfer (internal, not an external withdrawal) — these four are `extra_permissions` (non-blocking warning). Any other code, including whatever Bitget uses for Withdraw, is still reported as withdrawal possible (`withdrawal_enabled: null`) until confirmed |
| transactions (`/api/transactions`) | `GET /` · `/{id}` · `/crypto/{crypto}` · `/status/{status}` · `/stats/summary` — every Auto-Buy execution attempt (purchase_transactions): fill price, cost, fees, exchange, order id, and for withdrawals the address, network, fee and tx hash. `status` is pending \| completed \| failed |
| dashboard (`/api/dashboard`) | `GET /` (active/upcoming = the user's active Auto-Buy purchases, both types; holdings from purchase_transactions: `total_amount` = acquired NET of the trading fee charged in the coin, `on_exchange` = acquired minus what each real withdrawal took off the Bitget balance, `sent_to_wallet` = what those withdrawals delivered; see "Holdings arithmetic") · `/stats` · `/analytics/timeline?days=N` |
| service | `GET /health` (`schedulers`: pause switch + per-scheduler running/paused, last run, last result, consecutive failures for both the legacy recurring tick and the Auto-Buy job) · `GET /metrics` (Prometheus; keep private) |

Full request/response schemas with examples live in `/docs`.

## KYC flow

Register → `POST /api/kyc/initiate` (creates the Sumsub applicant, returns the
WebSDK token for the widget) → user completes verification in the widget →
Sumsub calls `POST /api/kyc/webhook` (HMAC-verified) → `kyc_status` becomes
`verified`/`rejected` and the user is emailed. **Purchases execute only for
verified users.** `kyc_status` cannot be set through the profile API.

## Error codes

| Code | Meaning |
|---|---|
| 400 | Request conflicts with state (duplicate email, nothing to update) |
| 401 | Missing/invalid/expired/revoked token, or wrong credentials |
| 404 | Not found — including other users' resources (no existence leaks) |
| 413 | Body over 1MB |
| 422 | Validation failure (amounts, crypto allow-list, addresses, password policy, duplicate active schedule) |
| 429 | Rate limit or account lockout — honor `Retry-After` |
| 500 | Unexpected; body carries `request_id` — quote it to support |

## Rate limits (per user when authenticated, else per IP)

login/register 5/min · verify/refresh/logout 10/min · key verification 3/min ·
purchase creation 20/min · transaction listing 30/min · dashboard 10/min ·
default 60/min. Login lockout: 5 failed attempts → 15 minutes.

## Auto-Buy purchases

The platform invents no minimum amount (since 2026-09-18; the earlier $10
constant is gone). The floor is Bitget's live figure for the mode: accumulate
purchases must only clear the pair's minimum order (`minTradeUSDT`, 1 USDT
today); withdraw-immediately purchases must also clear the chain's minimum
withdrawal plus fee at the live ask, with 1% headroom. Both checks run at
creation and again in the executor, and fail closed (503
`exchange_data_unavailable`, nothing created) when Bitget cannot be reached.

`/api/v2/purchases` buys `amount_usd` of a coin on one of the caller's
exchange accounts in one of two modes selected by `purchase_type`: `autobuy`
(default: each buy is withdrawn to `wallet_address` on `blockchain_network`)
or `dca` (coins accumulate on the exchange; a wallet is optional and, if
given, validated). `blockchain_network` is the exchange's own chain name as
returned by `GET /api/v2/market/coins/{coin}`; it defaults to the coin's
native chain (BTC → `BTC`, ETH/USDT/USDC → `ERC20`, SOL → `SOL`, XRP → `XRP`,
XLM → `StellarLumens`) and the address must match the chain's family
(bitcoin mainnet, EVM, Solana, Tron, XRP Ledger, Stellar). Chains that need
a destination tag / memo carry two more fields, see "Destination tag / memo".
Both modes share the same list, history and scheduler, and every execution
attempt records its `tx_type` (`buy`, and `withdrawal` as a separate row).
The boolean `withdraw_immediately` is accepted as a deprecated alias and
echoed in responses. A background job runs every 5 minutes
(`api/services/purchase_executor.py`; `api/tasks/purchase_scheduler.py` is a re-export shim): only KYC-verified, non-suspended owners
are processed; every attempt is recorded as a `purchase_transactions` row;
success advances `next_execution` (daily +24h, weekly +7d, monthly same day
next month, one-time completes); three consecutive failures mark the purchase
`failed`. Buys and withdrawals are simulated while `AUTOBUY_DRY_RUN=1`.
Only exchanges listed in `SUPPORTED_EXCHANGES` (default `bitget`) accept
keys. The Bybit and OKX code paths were removed on 2026-09-22 (audit C1); the Binance transport remains only for analytics prices until audit C3 retires it.

### Holdings arithmetic (since 2026-09-21)

Every quantity is derived from recorded rows, never from today's
configuration. A buy's **net amount** is the gross fill minus the trading fee
when Bitget charged it in the coin (fees in USDT or BGB leave the quantity
alone); proved against the live account on 2026-09-21: BTC 0.000197 bought,
fee 1.97e-07, balance 0.000196803, and likewise XLM 14.30568 and XRP
1.3320666. Transaction rows keep the **gross** fill in `amount_crypto` (what
Bitget's order history shows) and carry `net_amount` beside it; the tax CSV
has both `quantity` and `net_quantity`. Holdings, average buy price, P&L and
cost shares all use the net figure.

A successful, non-simulated withdrawal row records `amount_executed` (size
handed to Bitget), `withdrawal_fee` and `expected_received`, and the mode it
was sent under is read from those: `expected_received == amount_executed`
means **exclusive** (fee charged on top: the balance dropped by size + fee,
the wallet received size); `expected_received == amount_executed − fee` means
**inclusive** (the balance dropped by size, the wallet received size − fee).
`on_exchange` subtracts what left the balance; `sent_to_wallet` sums what the
wallet received. A row sent under one mode is never recomputed under another:
since migration 025 the executor stamps `withdrawal_fee_mode` on every
withdrawal row at send time (CHECK `exclusive` / `inclusive` / NULL) and
readers use the column; the `expected_received` derivation is the fallback for
rows written before 025 only. A NULL on a newer row is a bug, not a legacy
case. `BITGET_WITHDRAW_FEE_MODE` (exclusive) is confirmed only by the
first real withdrawal: compare `on_exchange` with Bitget's balance to the last
digit and record the result here. **Not yet done as of 2026-09-21.**

### Bitget market data

`api/services/bitget_market.py` caches Bitget's public spot symbols
(`GET /api/v2/spot/public/symbols`) and coin chains
(`GET /api/v2/spot/public/coins`) in Redis for one hour
(`bitget:market:symbol:{PAIR}`, `bitget:market:chains:{COIN}`). Field names
were verified against the live endpoints on 2026-09-11: symbols carry
`status`, `minTradeUSDT`, `maxMarketOrderValue`, `quantityPrecision`,
`takerFeeRate`; chains carry `chain`, `withdrawable`, `withdrawFee`,
`extraWithdrawFee` (fraction), `minWithdrawAmount`, `withdrawMinScale`,
`needTag`, `contractAddress`, `browserUrl`. Purchase creation and the
`PUT /{id}` mode switch validate against this data and answer 422 with
`{error, details}` where `error` is one of `pair_not_available`,
`pair_not_trading`, `below_minimum_order`, `above_maximum_order`,
`chain_not_available`, `chain_not_withdrawable` or
`below_minimum_withdrawal` (details include `minimum_usd`, the smallest
amount that clears minimum + fee at the live ask, plus 1% headroom for the
price moving between creation and execution), and 503
`exchange_data_unavailable` when Bitget cannot be reached and nothing is
cached (the purchase is not created). The executor re-checks the pair before
buying and the chain before withdrawing. The withdrawal size depends on
`BITGET_WITHDRAW_FEE_MODE`: `exclusive` (default) assumes Bitget charges the
fee on top of `size`, so the executor sends net minus fee (floored to
`withdrawMinScale`) and checks the account's available balance covers size +
fee before a live withdrawal; `inclusive` sends the net balance and Bitget
deducts the fee. The minimum applies to the size actually sent. Withdrawal
rows store both `amount_executed` (sent) and `expected_received`. A dry-run
withdrawal is presented with status `simulated` in the purchases API, the
transactions API (`status_filter=simulated`), emails and the tax CSV
(`withdrawal_status` column); storage keeps `success` + `raw_status =
simulated`. Crypto amounts are shown with at most 8 decimals, trailing zeros
trimmed. Purchase coins are BTC, ETH, USDC, SOL, XRP, XLM (USDT is the quote
coin; KORU was evaluated 2026-09-16 and excluded: Bitget lists it with no
chains and no USDT spot pair, so nothing can be delivered). `takerFeeRate` is
Bitget's public standard tier: the first live BTC fill (2026-09-16) paid 0.1%
against the quoted 0.2%, so the estimate is an upper bound for a discounted
account;
accumulate purchases without a wallet store no `blockchain_network` (null,
"held on the exchange"). Deleting an exchange key pauses its purchases with
`last_error` "Exchange key removed…", `exchange_key_removed: true`, and
resuming or force-retrying them answers 409 `exchange_key_removed`. Bitget only sends API withdrawals to addresses saved in
the user's Bitget address book, and withdraw-enabled keys must whitelist the
server's IP; both are explained on the Settings page.

## Email (Resend)

Every email the platform sends (welcome, email verification, password reset,
password-changed notice, KYC result, purchase executed/failed, first purchase,
milestones, weekly summary, notification-channel email) goes through one
module, `api/integrations/resend.py` (logger `autobuy.integrations.resend`).
Nothing about email is hardcoded; these variables drive it:

| Variable | Purpose |
|---|---|
| `RESEND_API_KEY` | Resend API key. Empty or a placeholder = email disabled: each send logs `email skipped (not configured)` at WARNING and is dropped. |
| `RESEND_FROM_EMAIL` | Sender for every email, e.g. `Minas AutoBuy <noreply@minastrade.com>`. The domain must be verified in Resend. **Verified today: `minastrade.com`.** `minastech.ca` is not verified and cannot send. |
| `RESEND_REPLY_TO` | Reply-To on every email; defaults to `SUPPORT_EMAIL`. |
| `SUPPORT_EMAIL` | Support inbox shown in footers and the Reply-To fallback. Never a sender. |
| `APP_BASE_URL` | Public origin of the web app; the only base for links in emails (reset, verification). Never the request Host. |
| `APP_NAME` | Product name in subjects and bodies (default `Minas AutoBuy`). |
| `LOG_RESET_LINKS` | `1` writes password reset links to the API log for local testing. Off by default; refused in production. |

Delivery failures are logged at ERROR with the Resend status and reason.
Fire-and-forget senders (purchases, KYC, campaigns, notifications) never
fail their caller; the verification and notification routes return 503 when
email is not configured and 502 when Resend rejects the send.

**Local testing:** set a real `RESEND_API_KEY` and a verified sender in
`.env`, `APP_BASE_URL=http://localhost:5173`, optionally `LOG_RESET_LINKS=1`,
then `POST /api/auth/forgot-password` and watch `docker compose logs -f api`
for `email sent: id=…`.

**Production requires** a real key, a verified-domain sender and an
`https://` `APP_BASE_URL` that is not localhost; the boot guard,
`scripts/validate_env.py` and `scripts/deploy.sh` all refuse otherwise.

**Switching domains later:** verify the new domain in Resend (Domains → add,
publish the DNS records, wait for "Verified"), change `RESEND_FROM_EMAIL`
(and `RESEND_REPLY_TO` if needed) in the server's `.env`, then
`docker compose --profile prod up -d api`.

## Client SDKs

Generated from `/openapi.json` and kept in [`clients/`](../clients/README.md):
`autobuy-client` (Python, sync + asyncio), `@autobuy/js-client` (ESM + types)
and `@autobuy/ts-client` (TypeScript). Each handles bearer auth, transparent
token refresh, typed errors, and ships a register → login → schedule example.

## Planned

WebSocket push for live transaction status.

## History and purchase events

`purchase_transactions` is the single source of truth for everything that shows
history: the Transactions API, dashboard holdings / monthly average / timeline,
analytics performance and comparison, tax CSV / PDF / performance reports, the
admin transaction audit and `failed_transactions_24h`, the weekly digest and the
first-purchase / milestone emails. The tax CSV has one row per successful buy:
`datetime_utc, crypto, quantity, fill_price_usd, usd_amount, fee, fee_currency,
exchange, order_id, purchase_type, tx_type, withdrawal_address, network,
withdrawal_fee, withdrawal_fee_currency, tx_hash, method`.

The Auto-Buy executor emits one event per attempt outcome — `buy_executed`,
`buy_failed`, `withdrawal_sent`, `withdrawal_failed` — and the purchase
notification email, lifecycle campaign emails, SMS/push dispatcher, Slack ops
alerts and outbound user webhooks (`purchase.<event>`, HMAC-signed) all hang
off those events. Dry-run emails are prefixed `[DRY RUN]`. The weekly digest
runs on the Auto-Buy scheduler (Mondays 09:00 UTC, one worker via advisory lock).

## Wallet-change hold (24 hours after a password reset or change)

`POST /api/auth/reset-password` and `POST /api/auth/change-password` set
`users.wallet_hold_until` to now + 24 h (migration 022) and
`wallet_hold_reason` (`password_reset` | `password_change`); the
password-changed email states when the hold ends, and `GET /api/user/profile`
returns both fields while the hold is active (null otherwise). During the
hold these answer 422 `{error: "wallet_change_hold", details: {action,
hold_until, reason, message}}`: creating a purchase with a wallet address
this user has never used (on any of their purchases or saved wallets),
changing the wallet on a purchase to such an address, switching a purchase
to withdraw-immediately (any address), and `POST /api/wallets` for a new
address. Addresses already used by the user are accepted, existing purchases
keep executing, and retry-withdrawal to the purchase's own address works.
The hold is stored on the user row, so it survives restarts and applies in
every worker. Support override: `POST /api/admin/users/{id}/lift-wallet-hold`
(admin only) with `{confirm_email, reason}` — the email must match exactly,
the reason (min 5 chars) records how the customer proved their identity;
the hold is cleared, an `admin_action` audit line `wallet_hold_lifted` is
written with the admin id, user, reason and time, a `wallet_hold_lifted`
row lands in the user's notification history, and the user is emailed.
409 when no hold is active.

## Purchase emails are queued

Since 2026-09-22 (audit C2) the three security emails — password reset,
password changed, withdrawal-address hold lifted — are queued the same way,
under the `security` category (6 attempts with backoff 30 s … 8 min, about
15 minutes in all) instead of one inline call to Resend. They leave on the
next `drain_email_outbox` run, i.e. within about a minute, and a provider
failure is retried rather than lost. The reset link and token are unchanged.
The `wallet_hold_lifted` audit row records `queued` rather than `sent`.


The executor never waits on the email provider. Purchase confirmations,
failures and campaign emails are rendered on the purchase path and pushed
onto a Redis outbox (`autobuy:email:outbox`, shared by all workers, survives
restarts); the scheduler sends them after each run and every minute
(`drain_email_outbox`), 20 s budget per drain and 10 s per email. A timed-out
or rejected email is re-scheduled with backoff (30 s, 1, 2, 4, 8 min — about
15 minutes in total): confirmations get 4 attempts, failure and security
emails 6. When the attempts are exhausted the message is logged at ERROR
with recipient and template, listed at `GET /api/admin/emails/undelivered`
(with the messages still retrying), recorded in the user's notification
history as `failed`, and counted towards the `email_delivery_failures`
admin alert (critical above `EMAIL_FAILURE_ALERT_THRESHOLD`, default 5 per
hour; one Slack notice per hour). Nothing in this path ever fails, retries
or delays a purchase. If Redis is unavailable, one 5-second inline attempt
is made and a loss is logged at ERROR. The queue is capped at 10,000
messages (earliest-due dropped, logged). Draining ignores the admin pause
switch: pausing stops trading, not notifications.
Security emails (password reset / changed) are sent directly by the request
that triggers them.

## Dry-run pricing

With `AUTOBUY_DRY_RUN=1` nothing is sent to the exchange, but each attempt is
priced from the venue's live ticker at the moment of the run: the best ask
(what a market buy would pay), or the last trade only when the venue reports
no ask. The price is stored unrounded as `fill_price`, the ticker snapshot is
logged on `autobuy.exchange_client`, and if no positive live price is
available the attempt fails with `PRICE_UNAVAILABLE` — there is no fallback price.

## Buy idempotency and transaction rows

Every attempt is a `purchase_transactions` row. The buy row (`tx_type: buy`)
is written with a unique `client_oid` and committed BEFORE the order is sent;
the fill (quantity, average price, quote spent, fee + currency) is read from
the exchange's order detail, never from the place-order response. If sending
raises, the executor looks the `client_oid` up on the exchange first: a fill is
recorded and never re-bought, "not found" is an ordinary failure (retried up to
3 times), and anything else parks the purchase as `needs_review` with a Slack
alert and no automatic retry. Before any retry, earlier attempts with an
unknown outcome are reconciled the same way.

A fill is recorded as a completed buy only when it passes `validate_fill`:
marked filled, amount and cost both positive, cost within
`FILL_TOLERANCE_PCT` (default 10%) of the amount requested, amount within
the tolerance of cost / price (the reference price read just before the
order, else the fill's own average) and internally consistent. Zero,
partial and implausible fills — on the direct path and on reconciliation —
park the attempt as `needs_review` with the Slack alert and never advance
the schedule. A failed attempt never advances the schedule either, spends
nothing, and carries no cost in the history: `total_invested_usdt`,
`average_purchase_usdt` and `completed_count` are computed over successful
buys only; `failed_count`, `pending_count` and `needs_review_count` are
separate; `total_transactions` counts every row for the table. A buy whose
amount is unknown or zero never reaches the withdrawal gateway: the
withdrawal row is written as failed and the purchase parked.

Warning band: a fill inside the parking tolerance but deviating more than
`FILL_WARN_PCT` (default 3%) from the expected amount is still recorded as a
successful buy, logged with expected vs actual figures, and counted in Redis
for a day; the `fill_deviations` admin alert is warning at 3 and critical at
10 in 24 h, details at `GET /api/admin/fills/deviations`.

Retry policy: a failed attempt leaves `next_execution` in the past, so the
next scheduler tick (5 min) retries; after 3 failed attempts in a cycle a
recurring purchase MISSES that cycle and moves on to its next normal
interval (`last_error` says so, the customer's email says the next attempt
time, Slack says "cycle MISSED"); after 3 missed cycles in a row it is
stopped (status `failed`, `last_error` "PURCHASE STOPPED", Slack and the
`stopped_purchases` admin alert). A one-time purchase is stopped after its 3
attempts. A success resets the streak. `missed_cycles` and
`purchases_needing_review` are admin alerts too.

Customer emails on failed buys follow the cycle, not the attempt: nothing
on attempts 1 and 2, one "Purchase missed" email when the cycle's attempts
are exhausted (naming the next attempt), one "purchase has been stopped"
email when it stops. Slack gets one line per run for failed attempts and one
per stopped purchase. Critical admin alerts are emailed to `SUPPORT_EMAIL`
and every admin every 15 minutes (once per condition per 6 h).

Way out of `needs_review`: `GET /api/admin/purchases?status=needs_review`
lists parked purchases with their attempts; `GET /api/admin/purchases/{id}/review`
asks the exchange, by our client id, what became of each order (state, fill,
and whether the fill passes the guard); `POST /api/admin/purchases/{id}/resolve`
with `{action, note}` records the fill (only when the exchange shows FILLED
and the guard passes; withdraw-immediately purchases then withdraw), marks it
not sent (only when NOT FOUND or CANCELLED; the purchase is due again) or
cancels. Every resolution is audited. `force-retry` refuses `needs_review`
purchases because re-arming could buy twice.

For withdraw-immediately purchases the withdrawal is its own row
(`tx_type: withdrawal`, `parent_transaction_id` -> the buy) carrying the
address, chain, withdrawal id, fee and tx hash; the tax CSV reports them on
the buy lot. A failed withdrawal parks the purchase as `withdrawal_failed`
(coins stay on the exchange, the buy is never repeated); a rejection for
missing travel-rule / identity data parks it as `needs_info` and emails the user.
Neither is retried automatically. `POST /api/v2/purchases/{id}/retry-withdrawal`
sends the stranded coins again (same checks as creation: wallet, chain,
Bitget minimum) as a new withdrawal row under the same buy; on success the
purchase is `active` again and its schedule resumes from now. The admin
`force-retry` refuses these two statuses for the same reason.

### Withdrawal confirmation (poller)

Sending a withdrawal only returns Bitget's withdrawal id. A second scheduler
job, `poll_withdrawals` (every 10 minutes, same Redis pause switch, own
advisory lock, heartbeat under `/health` `schedulers.withdrawals`), reads
`GET /api/v2/spot/wallet/withdrawal-records` for every sent withdrawal of the
last 7 days whose `raw_status` is not final and stores the on-chain hash
(`tradeId`), the final fee and Bitget's status. `success` settles the row;
`wallet_fail` / `cancel` / `reject` mark it failed, park the purchase as
`withdrawal_failed` and emit `withdrawal_failed` (email + Slack). A withdrawal
still unresolved after 7 days is marked `unconfirmed` and flagged on Slack.
Dry-run rows (`raw_status: simulated`) and non-Bitget venues are skipped.

### Destination tag / memo (XRP Ledger, Stellar) — migration 023

Live Bitget reports `needTag: true` for the `XRP` and `StellarLumens` chains
(verified 2026-09-16). On those chains an exchange or custody address is a
shared account: a withdrawal without the right destination tag (XRP) or memo
(Stellar) lands in the pool with nothing linking it to the customer. The API
therefore stores two facts and refuses to guess:

* `destination_type` — `self_custody` (a wallet the customer controls; no
  tag) or `exchange` (an exchange / custody account; tag required). Stored
  on `wallets.destination_type` and `purchases.wallet_destination_type`.
* `memo` — the tag / memo itself (`wallets.memo`, `purchases.wallet_memo`;
  each withdrawal row records what was sent in
  `purchase_transactions.withdrawal_memo`). XRP: unsigned 32-bit integer in
  digits. Stellar: id memo (unsigned 64-bit) or text memo up to 28 bytes.

One rule (`api/utils/chains.check_destination`) is applied by
`POST /api/v2/purchases`, `PUT /api/v2/purchases/{id}` (wallet change, memo
edit, switch to `autobuy`), `POST /api/v2/purchases/{id}/retry-withdrawal`,
`POST /api/wallets` and the executor. 422 errors: `destination_type_required`
(tag chain, question never answered), `memo_required` (exchange without a
tag), `invalid_memo`, `memo_not_expected` (self-custody with a tag),
`memo_conflict` (typed tag differs from the one inside an X-address / muxed
address, or self-custody chosen for such an address), and on other chains
`memo_not_used` / `destination_type_not_used`. An XRP X-address (`X…`) or a
Stellar muxed address (`M…`) carries its tag: it is decoded, the base
(classic `r…` / `G…`) address is stored and the tag becomes the memo.

The executor refuses to send on a tag chain when the purchase never recorded a
destination type or is an exchange destination without a memo: the withdrawal
row is `failed`, the purchase parks as `withdrawal_failed` with the reason
(code `TAG_REQUIRED`), no re-buy, and the memo can be added with `PUT` before
`retry-withdrawal`. The memo is passed to Bitget as the withdrawal body field
`tag` — **that request field name comes from Bitget's documentation and SDK and
has not been verified against a real tagged withdrawal yet** (no live XRP/XLM
withdrawal has been sent as of 2026-09-16); `needTag` on the coins endpoint is
live-verified.

`GET /api/v2/market/destination?chain=&address=&destination_type=&memo=`
returns the same verdict before a form submits (`ok`, `error`, `message`,
`needs_tag`, `tag_label`, `embedded_tag`) and, on tag chains, asks the public
ledger whether the account exists (`funded`; XRPL `account_info` →
`actNotFound`, Horizon 404, both live-verified). A never-funded address sets
`warning` (a typo looks the same as a brand-new wallet) but never blocks:
Bitget's minimums exceed the XRPL (1 XRP) and Stellar (1 XLM) reserves, so
the first send funds the account. `GET /api/v2/market/coins/{coin}` chains
carry `need_tag` and `tag_label` ("destination tag" / "memo").

### Travel-rule / beneficiary details

`travel_rule_info` (optional on create and update; `{}` clears it) stores
`first_name`, `last_name`, `identity_type`, `company_name`, `area_code`,
`member_code`. When present they are sent with Bitget withdrawals as
`firstName`, `lastName`, `identityType`, `companyName`, `areaCode`,
`memberCode`. Nothing is prefilled: the user model stores no KYC names.
