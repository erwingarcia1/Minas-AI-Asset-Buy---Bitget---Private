# AutoBuy Crypto API — Reference

Automated cryptocurrency purchasing: users schedule recurring USDT buys
(daily / weekly / biweekly / monthly) that the platform executes on Binance,
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

# 4. Store + verify a Binance API key (spot-trade only; no withdrawal permission)
KEY=$(curl -s $BASE/api/exchange/keys -H "$AUTH" -H 'content-type: application/json' \
  -d '{"exchange":"binance","api_key":"<key>","api_secret":"<secret>"}')
curl -s -X POST $BASE/api/exchange/keys/$(echo $KEY | jq -r .id)/verify -H "$AUTH"

# 5. Schedule $100 of BTC weekly
curl -s $BASE/api/purchases/ -H "$AUTH" -H 'content-type: application/json' \
  -d '{"crypto":"BTC","amount_usdt":100,"frequency":"weekly"}'

# 6. Watch it happen
curl -s $BASE/api/transactions/ -H "$AUTH"
curl -s $BASE/api/dashboard/ -H "$AUTH"
```

## Authentication

JWT bearer tokens. `POST /api/auth/login` returns an **access token**
(30 min) and a **refresh token** (7 days, revocable):

- Send `Authorization: Bearer <access_token>` on every protected call.
- On 401, exchange the refresh token at `POST /api/auth/refresh` for a new pair.
- `POST /api/auth/logout` revokes a refresh token immediately.
- `POST /api/auth/change-password` invalidates **every** token issued before it.
- Refresh tokens are rejected as access tokens and vice versa.

## Endpoints by module

| Module | Endpoints |
|---|---|
| auth (`/api/auth`) | `POST /register` · `/login` · `/verify` · `/refresh` · `/logout` · `/change-password` · `/forgot-password` (emails a 1-hour single-use link; always `{sent: true}`) · `/reset-password/validate` (`{token}` → 200 `{valid: true}` / 400 `{valid: false}`, does not consume the token) · `/reset-password` (`{token, new_password}` → 204; signs out every other session) |
| user (`/api/user`) | `GET/PUT /profile` · `GET /{user_id}` |
| kyc (`/api/kyc`) | `POST /initiate` · `GET /status` · `POST /create-applicant` · `GET /applicant/{id}/status` · `POST /webhook` (Sumsub, HMAC) |
| auto-buy (`/api/v2/purchases`) | `POST /` (validates wallet address vs coin chain + network, exchange network support, account ownership) · `GET /` (with latest transaction) · `GET /{id}` (full history) · `GET /{id}/transactions?status_filter=` · `PUT /{id}` (`status: paused|active`, amount, frequency) · `DELETE /{id}` (cancel, history kept) |
| wallets (`/api/wallets`) | `POST /` (format must match network; Bitcoin testnet/mainnet distinguished; Ethereum/Solana valid on both) · `GET /?blockchain_network=` · `DELETE /{id}` |
| email (`/api/email`) | `POST /send-verification` · `POST /send-notification` (own address only; admins may notify anyone) |
| webhooks (`/api/webhooks`) | `POST /sumsub` (HMAC via `x-payload-digest` / `x-payload-digest-alg`) · `POST /resend` |
| exchange (`/api/exchange`) | `POST/GET /keys` · `DELETE /keys/{id}` · `POST /keys/{id}/verify` |
| purchases (`/api/purchases`) | `POST/GET /` · `GET/PUT/DELETE /{id}` · `PATCH /{id}/toggle` |
| transactions (`/api/transactions`) | `GET /` · `/{id}` · `/crypto/{crypto}` · `/status/{status}` · `/stats/summary` — every Auto-Buy execution attempt (purchase_transactions): fill price, cost, fees, exchange, order id, and for withdrawals the address, network, fee and tx hash. `status` is pending \| completed \| failed |
| dashboard (`/api/dashboard`) | `GET /` (active/upcoming = the user's active Auto-Buy purchases, both types; holdings and averages from purchase_transactions) · `/stats` · `/analytics/timeline?days=N` |
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

`/api/v2/purchases` buys `amount_usd` of a coin on one of the caller's
exchange accounts in one of two modes selected by `purchase_type`: `autobuy`
(default: each buy is withdrawn to `wallet_address` on the declared network)
or `dca` (coins accumulate on the exchange; the wallet is kept on file for
later withdrawals). Both modes require a validated wallet address and a
network, share the same list, history and scheduler, and every execution
attempt records its `tx_type` (`withdrawal` or `accumulation`). The boolean
`withdraw_immediately` is accepted as a deprecated alias and echoed in
responses. Supported coins and address chains: BTC (bitcoin), ETH/USDT/USDC
(ethereum), SOL (solana). A background job runs every 5 minutes
(`api/tasks/purchase_scheduler.py`): only KYC-verified, non-suspended owners
are processed; every attempt is recorded as a `purchase_transactions` row;
success advances `next_execution` (daily +24h, weekly +7d, monthly same day
next month, one-time completes); three consecutive failures mark the purchase
`failed`. Buys and withdrawals are simulated while `AUTOBUY_DRY_RUN=1`; real
withdrawals are implemented for Binance only.

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
