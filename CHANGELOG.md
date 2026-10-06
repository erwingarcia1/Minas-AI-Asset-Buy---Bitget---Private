# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.4] — 2026-10-06: pre-buy low-balance warning (PR #55)

### Added

* **Low-balance warning before a scheduled buy.** A new read-only scheduler job
  (`low_balance_check`, every 30 minutes, advisory-locked) looks at every
  active purchase whose next run is about a day away (23–25 h) or about two
  hours away (1–3 h), reads the available spot USDT on its exchange account
  (one read per account per pass, never an order call) and, when it is below
  the sum of what every in-window purchase on that account needs (each amount
  plus 1%), warns once per purchase per run, one email per account: email "Top up before your next
  AutoBuy purchase" (purchases on that account, run time in Eastern Time with UTC in brackets,
  balance, shortfall, how to move USDT to spot, unsubscribe link), a banner on the
  dashboard and on the purchase row ("Balance low for your next buy on …"),
  and one masked line in the admin Telegram chat. The two-hour window also
  warns when the day-before check never ran. A later check that finds enough
  balance clears the banner. An unreadable balance goes to admins only, never
  to the customer. Purchases that are paused, cancelled, completed, already
  past their run time, owned by a suspended or unverified user, or on an
  unverified key are skipped. The executor, poller and withdrawal code are
  untouched; the job's only writes are two bookkeeping columns.
* Migration 026: `purchases.low_balance_alerted_for` / `low_balance_alerted_at`
  (nullable, no backfill, no index).
* Notification preference `email_low_balance` (on by default) in the existing
  preferences JSON, exposed on `GET/PUT /api/notifications/preferences` and in
  Settings → Email notifications.
* `GET /api/v2/purchases` rows carry `low_balance_for_next_run` and
  `low_balance_alerted_at`; `GET /api/dashboard/stats` carries `low_balance`
  (the flagged purchases). Six scheduler jobs are now registered.

## [0.7.3.1] — 2026-10-06: sign-out from a stale tab (issue #64, PR #65)

### Fixed

* **Signing out from a tab whose CSRF value went stale now signs out for
  real.** When another tab had rotated the cookies, the stale tab's logout
  was refused with 403 and the web app showed the login page anyway, leaving
  the refresh cookie valid on that computer. Logout now revokes the session
  whenever the refresh cookie holds a valid token, whatever the CSRF header
  says (the cookies are SameSite=Lax, so a cross-site POST cannot carry them
  and a forged sign-out is not possible); without a valid cookie the CSRF
  check applies as before. The web app no longer pretends: a refused sign-out
  is retried once with a freshly refreshed CSRF value, and if that fails too
  it shows "Sign-out failed, try again." and keeps the session visible.

## [0.7.3] — 2026-10-05: refresh rotation grace window (PR #57)

### Fixed

* **Two tabs refreshing at the same moment no longer sign the user out.** The
  refresh token is single use (rotation); a second tab presenting the same
  cookie a moment later got 401 and the cleared cookies signed both tabs out.
  Now exactly one request rotates the token (an atomic SET NX claim in Redis)
  and the other gets the very same new session back: same access token, same
  refresh cookie, same CSRF value, cached encrypted (Fernet, the app's own
  helper) for 30 seconds under the id of the token it replaced. The grace
  covers only the immediately previous token: the next rotation drops it, a
  token two rotations old is refused as before, logout and a password change
  drop it, and a password change made elsewhere is caught by the cached
  session's issue time. A real refusal still clears the cookies; a grace
  response is an ordinary 200 with Set-Cookie, so the web client needed no
  change. Refresh tokens now carry `prv`, the id of the token they replaced.
* **The CSRF cookie rotates with the refresh cookie, and the window covers it
  too.** Found while preparing the release: a second tab that built its
  request with the previous CSRF value, but whose browser attached the
  rotated cookies when it was sent, got a 403 and was signed out. The
  rotation's cached entry now remembers the CSRF value it validated; inside
  the window such a request gets the same cached session. Any other mismatch,
  and the same header once the window is gone, stay a 403.
* Tokens issued before this release (no `prv` claim) refresh, get the window,
  and die on logout and password change exactly like new ones (tested).
* The committed OpenAPI spec and SDKs were regenerated from the merged code.

## [Unreleased] — 2026-10-01: withdraw gate, remaining paths (PR #54)

### Fixed

* Five more paths returned a withdraw-mode purchase to the scheduler without
  the private-beta gate: resuming a paused one (`PUT … status: active`), the
  admin "mark not sent" resolution, unsuspending an account, a KYC override
  back to verified, and re-verifying an exchange key. All five are now judged
  by the purchase owner. Resume answers 403 with the beta message plus "Switch
  this purchase to accumulate first, then resume it." (switching and resuming
  in one call works). Unsuspend and KYC-to-verified are refused while the
  account owns ACTIVE withdraw-mode purchases, with the purchase ids listed;
  unsuspend accepts `{"pause_withdraw_mode": true}` to pause those purchases
  and reactivate the account instead. Key re-verify is refused, before any
  exchange call, while that key has active withdraw-mode purchases. The web
  purchase list no longer offers Resume on such a purchase to users who are
  not allowed; "Switch to accumulate" stays. Executor and withdrawal code
  untouched; existing purchases are not modified by any refusal.

## [Unreleased] — 2026-10-01: landing follow-ups (PR #52)

### Changed

* Every "Request access" button carries "or email support@minastech.ca" as
  visible text (from `/api/public/meta`, with the existing fallback).
* Below 640px the how-it-works diagram renders top to bottom (a second SVG
  layout; the wide one is hidden), so phones no longer get a scrolling box;
  reduced-motion and offscreen-pause behaviour unchanged.
* Anonymous visitors no longer call the refresh endpoint at startup: with no
  session cookie the app goes straight to anonymous. A returning session still
  refreshes. Lighthouse best practices 96 → 100 (the console-error audit was
  the anonymous 403).
* Header logo keeps its 28px box on narrow screens.

## [Unreleased] — 2026-09-30: private-beta withdraw gate (PR #50)

### Changed

* **Withdraw-immediately purchases are off during the private beta.** New
  settings `WITHDRAWALS_ENABLED` (default 0) and `WITHDRAW_ALLOWED_EMAILS`
  (comma-separated). The API refuses every path that creates or switches a
  purchase into withdraw mode, retries a withdrawal, or re-arms a withdraw-mode
  purchase, unless withdrawals are enabled globally or the purchase OWNER's
  email is allowlisted (never the acting admin), with one message: "Sending
  coins to your own wallet isn't available during the private beta. Your
  coins stay in your Bitget sub-account." The API default `purchase_type` is
  now `dca` (accumulate). Admin resolve gains `keep_on_exchange`: record the
  fill, keep the coins on Bitget, switch the purchase to accumulate, so the
  review queue never becomes unresolvable. The profile exposes
  `withdrawals_allowed`; the web app hides the withdraw toggle (off by default
  for everyone), "Switch to withdraw", "Retry withdrawal" and the withdraw key
  guidance for users who are not allowed, and shows "Sending to your own
  wallet is coming later. For now, coins stay in your Bitget sub-account."
  Existing withdraw-mode purchases are not changed. Executor, withdrawal
  poller and withdrawal code untouched.
* Landing copy no longer implies wallet sending is available.

## [Unreleased] — 2026-09-30: fee wording (PR #51)

### Changed

* The purchase form no longer shows a fee percentage. It showed Bitget's
  public tier taker rate for the pair (0.20% for BTCUSDT) while the account's
  first live buy on 2026-09-16 was charged 0.10%, taken in the coin bought
  (1.97e-07 BTC on 0.000197 BTC), not in USDT. The form and the Settings key
  guidance now say: "Bitget's trading fee applies, taken from the coin you
  receive." The landing page already said so.

### Noted, no code change

* The fill guard (`validate_fill`) compares the gross filled amount
  (`baseVolume`) against cost ÷ price and does not subtract the trading fee;
  a fee taken in the base coin is recorded separately and only the holdings
  maths nets it out. At the current ±10% tolerance a 0.1–0.2% fee cannot
  trip the guard, so this is acceptable; it is not fee-aware and would need
  revisiting if the tolerance were ever tightened below about 1%.

## [Unreleased] — 2026-09-28: public landing page, private-beta gate, ticker (PR #48)

### Added

* **Landing page at `/`** (lazy chunk, 15 KB / 4.7 KB gzipped; the app's chart
  code is never loaded): header, hero with the "Private beta · by invitation"
  badge, how-it-works with one inline-SVG loop (transform/opacity keyframes,
  paused offscreen, static under prefers-reduced-motion, fixed aspect ratio),
  safety, supported coins and fees, setup guide with a copy-to-clipboard
  server IP, keyboard-accessible FAQ, final call to action, footer with
  Terms/Privacy placeholders (`TODO(owner): legal text`), the risk line and
  the copyright year. Signed-in visitors go straight to the dashboard; a
  returning session waits for the silent refresh so the landing page never
  flashes. Request access is a mailto to the support address.
* **Private-beta ticker** on every page (`VITE_BETA_TICKER=0` removes it at
  launch): transform-only seamless loop, brand-800 with white text (13.7:1),
  pause on hover/focus and a visible pause/play button, static under reduced
  motion, read once by screen readers, not dismissible.
* **`GET /api/public/meta`** (anonymous, cacheable 5 min, 60/minute): coins,
  exchanges, support email and the server's public IP (`PUBLIC_SERVER_IP`,
  new setting). Settings shows that IP instead of its placeholder.
* **Signup gate**: registration needs `SIGNUP_INVITE_CODE` whenever one is
  configured and always in production (empty closes signup there);
  constant-time comparison; one vague 403 for missing, wrong or unconfigured
  code, checked before the email lookup; the code is never logged. Signup
  asks for the code. Admin overview shows signups per day for 14 days from
  `users.created_at` (no migration) so a leaked code is visible.
* Dashboard: helper text under Holdings ("AutoBuy's holdings count only what
  it bought for you…"). `robots.txt`.

## [Unreleased] — 2026-09-28: Telegram alerts for purchase and withdrawal events (PR #47)

### Added

* **Telegram ops alerts** (`api/services/telegram.py`), additional to the
  email alert digest and Slack, never a replacement. One message per event,
  sent after the database write: purchase completed (coin, USDT, quantity,
  fill price, masked customer, whether it will be withdrawn), purchase failed
  or stopped (reason, attempt), purchase parked for review (with "needs admin
  review"), withdrawal sent, confirmed (tx id), failed, needs information.
  Emails show two characters plus the domain, addresses the first 6 and last
  4 characters; error reasons are scrubbed of emails and long ids; never an
  API key, secret, token or memo. Fully non-blocking: 5 s timeout, one retry
  on a transport error only, every path wrapped so a Telegram failure logs
  one WARNING and cannot reach the executor or change an outcome; both
  settings unset = silent no-op. The bot token never reaches a log line (a
  redaction filter scrubs httpx's request-URL log). Rate limit
  `TELEGRAM_MAX_PER_MINUTE` (default 20, excess dropped and counted); a burst
  of identical failures (same reason) collapses into one message plus a
  "repeated N more times in this run" summary at the end of the scheduler
  run. Settings `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `TELEGRAM_MAX_PER_MINUTE` are forwarded by compose and documented in
  `.env.example` (the env-plumbing test covers them).
* `POST /api/admin/telegram/test` (admin only, 3/minute): sends one test
  message and reports `configured`, `sent` and Telegram's error description,
  never the token.

## [Unreleased] — 2026-09-25: brand palette and link previews (PR #46)

### Changed

* **Brand purple across the app.** The tailwind `brand` scale is now built
  from the symbol's deep purple #543477 (brand-600; tints towards white,
  shades towards black), so primary buttons and their hover/disabled states,
  focus rings, links, the active nav item, the admin tabs, the loading spinner,
  info alerts and the active/autobuy chips take the purple. Native checkboxes
  and radios use it as their accent colour. The warm tan #87735b is a sparing
  secondary accent (the "accumulate" chip). Status colours (green / amber /
  red, the verified badge) are unchanged. The portfolio chart's value area is
  #543477 (was #1b5cf5). SOL's asset colour moved from violet #7c3aed to pink
  #db2777 so it stays distinct from the brand purple; other asset colours
  unchanged. Contrast (WCAG AA): white on brand-600 9.8:1, links 9.8:1, active
  nav 10.5:1, chips 11.2:1, focus ring 6.3:1.

### Added

* **Link previews.** `og-image.png` (1200×630) and `og-image-square.png`
  (1200×1200): the symbol beside "AutoBuy" on white. `index.html` carries the
  Open Graph and Twitter Card tags with absolute
  `https://autobuy.minastrade.com/…` URLs, a plain description, and the title
  "AutoBuy", so any shared URL (login page included) unfurls with the logo.

## [Unreleased] — 2026-09-25: AI Wealth Builder symbol as the app mark (PR #45)

### Changed

* **App mark and favicon.** The AI Wealth Builder symbol (source of truth
  `brand/aiwb-logo.png`, an 8625×8625 RGBA PNG; symbol only, never the
  wordmark — the app is still AutoBuy) replaces the blue "A" mark in the
  header and on the login/signup/reset pages, beside the AutoBuy wordmark at
  the same size and position, wordmark in the symbol's deep purple #543477
  (tailwind `brand-ink`, also the browser theme colour). Icons are transparent
  PNGs resampled from the source: 512/192/180/64 keep the gradient
  (#543477 → #61446f → #87735b); 16 and 32 px and `favicon.ico` are a flat
  deep-purple silhouette because the gradient washes out at that size.
  `manifest.json` (name AutoBuy Crypto, short_name AutoBuy, 192/512 icons) and
  an apple-touch-icon are new; `favicon.svg` is gone. Button and link colours
  are unchanged.

## [Unreleased] — 2026-09-23: Settings credentials out of autofill (PR #44)

### Changed

* **Exchange API key fields no longer look like a login to the browser.** The
  API secret is a masked `type="text"` input (`SecretInput`: CSS
  text-security with a show/hide button) instead of `type="password"`, so
  Chrome's password manager neither captures it nor offers to save it. API
  key, secret and passphrase carry `autocomplete="one-time-code"`,
  autocorrect/autocapitalize off, spellcheck off and the 1Password / LastPass /
  Bitwarden ignore attributes; their ids and names are
  `exchange_credential_public` / `_private` / `_phrase`; the fields are no
  longer inside a `<form>` and "Save key" is a plain button. The change
  password form is untouched (real password fields, `current-password` /
  `new-password`).

### Fixed

* **Direct load of /settings crashed.** Settings declared its `verifying`
  state after the early "no user yet" return, so opening `/settings` by URL
  (reload or bookmark) rendered Loading with one hook fewer and then failed
  with React error 310 once the profile arrived; reaching Settings through the
  app never triggered it. Introduced in 6f3aeec on 2026-09-11 (v0.5.0), live on
  the Tokyo server from the first server deploy (v0.6.0, 2026-09-17) until this
  fix; reproduced on the live site on 2026-09-23. Regression test renders with
  no user, then with one.

## [Unreleased] — 2026-09-22: dashboard charts (PR #42), deployed

Merged as 97759aa and deployed to the Tokyo server on 2026-09-22 19:27 UTC
with `scripts/deploy.sh platform --branch main --web ~/web-charts` (run on the
server). Verified after the deploy: health commit 97759aa, alembic 025,
dry_run false, holdings and the 8 transaction rows byte-identical to the
pre-deploy baseline (pre-deploy dump `~/backups/pre-charts-20260922T192446Z.sql.gz`),
BUILD_INFO through nginx and through the public host, five jobs registered,
purchase and withdrawal jobs ran ok, unfiltered log scan clean (the only
warnings were two 401s from a revoked test session). Live site as the owner's
account: donut total, performance market value and chart final point all
26.38 USDT on the same load; every range rendered; the first cold load made
16 Bitget candle calls (one per cached page), the second load made none; the
new endpoint answered 200 on every request; the old
`/api/dashboard/analytics/timeline` still answers.

### Added

* **Portfolio value chart** replaces "Invested — last 30 days" on the dashboard:
  headline value and change over the range (1D · 7D · 30D · 90D · All), a
  filled value area with a dashed step line for the amount invested, buy
  markers, crosshair with the buys of that bucket, touch drag on mobile. The
  change figure excludes what buys made in the range added (a simple return,
  not time-weighted) and says so. Read-only endpoint
  `GET /api/dashboard/portfolio/series?range=` (`api/services/candles.py`,
  `api/services/portfolio_series.py`): holdings at each point are the net
  acquired quantities of the buys executed by then × Bitget candle closes
  (15min / 1h / 4h / 1Dutc, pages cached in Redis per pair: one bucket for the
  newest page, 24 h for closed pages, never per user); a bucket with no candle
  carries the previous close forward and the response counts them
  (`carried_forward`); the final point uses `market_data.get_price` summed the
  way the performance card sums it, so the chart's last value, the allocation
  total and the performance market value agree by construction. 503 with
  `exchange_data_unavailable` when Bitget is unreachable and nothing is
  cached; the chart shows an unavailable state and the rest of the dashboard
  renders.
* **Allocation donut** replaces the "Share of invested value" line in the
  performance card: share of current value by default, cost on a labelled
  toggle, hover/focus details (asset, acquired quantity, USDT, share), a text
  legend, one colour per asset (`frontend/src/utils/assetColors.ts`, also used
  in the performance table), more than six assets collapse into "Other".
* Dashboard refresh policy (no polling loop): the charts and performance
  figures refetch when the tab regains visibility or focus, right after a
  purchase is created, changed or deleted anywhere in the app, and at most
  every 5 minutes while the tab stays visible (`usePortfolioRefresh`).
* Root `bitget_client.py`: `get_candles()` for the public candle endpoints
  (additive; no money path touched).

## [Unreleased]

### Added

* **Public HTTPS at https://autobuy.minastrade.com.** New `caddy` service in the
  prod compose profile (`deployment/caddy/Caddyfile`): automatic Let's Encrypt
  certificate, http → https redirect, HSTS on the static SPA as well as the
  API, proxying to the existing `web` container. It is the only service
  published on a public interface; the tailnet URL still works.

### Fixed

* **Per-IP rate limits were one bucket for everyone.** Behind the nginx `web`
  container the API saw every visitor as the container's address (verified in
  the production access log), so `5/minute` on login was shared by all users.
  Caddy now discards client-sent `X-Forwarded-*`, nginx resolves the real
  address (`real_ip_header`) and overwrites — never appends —
  `X-Forwarded-For`, and gunicorn trusts it (`--forwarded-allow-ips "*"`; safe
  because the API port is loopback-only). Pinned by `tests/test_public_proxy.py`.

### Changed

* **The production EC2 box has an Elastic IP, `16.76.92.209`** (assigned
  2026-09-21). `scripts/server_ssh.sh` and `scripts/remote_deploy.sh` default
  to it; README, `docs/DEPLOYMENT.md` (new "Production host" table),
  `docs/MEME_BUY.md` and `deploy/AWS_EC2.md` name it in place of the
  auto-assigned `18.179.57.78`, which no longer reaches the box. Exchange API
  key allowlists must name the new address. Public hostname to be served:
  `autobuy.minastrade.com` (A record → the Elastic IP).

## [Unreleased] — 2026-09-22: Batch C1 + C2 of the codebase audit (PR 4)

### Removed (C1)

* Bybit and OKX: the root `bybit_client.py` / `okx_client.py` and their
  tests, the eager imports, the `hasattr` and `TypeError` fallbacks and the
  "other venues" permission branch in `ExchangeClient`, their error-code
  tables in key verification, the OKX passphrase rule, and
  `WITHDRAW_NETWORK_CODES` in the gateway (the withdrawal network is the
  purchase's chain, as it always was for Bitget). Bitget is the only venue a
  key can be created for; the Binance transport stays only for analytics
  prices until C3. `tests/api/test_bitget_wire.py` pins the exact requests
  Bitget receives (market buy, lookup by client id, fill by order id, coin
  info, withdraw with and without tag, withdrawal records) and passes
  unchanged before and after.

### Changed (C2)

* Password reset, password changed and wallet-hold-lifted emails go through
  the email outbox under the `security` category (6 attempts, ~15 minutes)
  instead of one inline Resend call with no retry. Latency: from inline during
  the request to the next `drain_email_outbox` run (every minute). Link and
  token unchanged. The `wallet_hold_lifted` notification audit row now records
  `queued` instead of `sent`.
* Production note (2026-09-22): Resend answers 429 `daily_quota_exceeded` on
  the production account; no email has been delivered since 2026-09-21 and
  three purchase emails sit in the dead-letter list. C2 makes security emails
  survive such a window for about 15 minutes; it does not fix the quota.

## [Unreleased] — 2026-09-22: Batch B3 of the codebase audit (PR 3): the executor split

Move only. `api/tasks/purchase_scheduler.py` (1,146 lines, seven concerns) is
now three modules, every function, class and constant moved as an exact
source segment (52 names; source and AST hashes identical before and after):

* `api/services/exchange_gateway.py` — `ExecutionError`, the `ExchangeGateway`
  protocol, `_client_for`, `tag_requirement`, `ExchangeClientGateway`.
* `api/services/purchase_executor.py` — due-purchase selection (SKIP LOCKED),
  the fill guard, reconciliation before retry, `execute_one` (client_oid
  committed before the order is sent), withdrawals and the memo backstop,
  withdrawal polling, the batch driver with its advisory lock, run-result
  recording.
* `api/tasks/scheduler.py` — the APScheduler job wrappers, `build_scheduler`,
  `job_event_listener`.

`api/tasks/purchase_scheduler.py` stays as a shim that re-exports everything
and forwards attribute writes to the owning modules, so every existing import
and every monkeypatch (`ps._client_for`, `ps.execute_due_purchases`, …) still
reaches the real code. The APScheduler job ids, triggers, misfire settings and
coalescing are unchanged; only the function reference of each job moves from
`api.tasks.purchase_scheduler:` to `api.tasks.scheduler:`. The jobstore is the
in-memory default, so no persisted job needs to resolve the old path.

## [Unreleased] — 2026-09-22: Batch B of the codebase audit (PR 2: B1, B2, B4, B5)

No behaviour change; the OpenAPI document is byte-identical before and after.

### Changed

* **B1** `api/services/binance_client.py` → `api/services/exchange_client.py`;
  `BinanceClient` → `ExchangeClient`, `BinanceClientError` →
  `ExchangeClientError`. The old module path and class names remain as a
  one-release alias (`api/services/binance_client.py` re-exports; patching
  attributes there does not affect the real module). Prometheus metrics
  renamed: `binance_api_calls_total` → `exchange_api_calls_total`,
  `binance_api_duration_seconds` → `exchange_api_duration_seconds`; the timer
  `metrics.time_binance` → `time_exchange` (old name aliased for one
  release). `docs/ALERTING.md` rule updated; saved Prometheus rules must be.
* **B2** shared helpers in `api/routes/_deps.py`: `user_or_404` (was pasted
  six times), `purchase_or_404` (three loaders with the same 404), `gateway`
  (was defined twice), `PrecisionMemo` as a request dependency (was four
  copies of the same memo). One read-through cache in
  `api/services/cache.py`; `bitget_market` keeps fail-closed
  (`MarketDataUnavailable`) and `market_data` keeps degrade-to-None, exactly
  as before — pinned by `tests/api/test_cache_policy.py` through both real
  callers (read failure → producer runs; producer failure → refused / None;
  write failure → value still returned).
* CI: the push trigger now matches branch names with a slash (`**`).
* **B4** `api/routes/admin.py` split into `api/routes/admin/{users,purchases,ops}.py`
  with `_deps.py`; purchase request/response schemas moved to
  `api/schemas/purchases.py`; Base58 decode/encode and the double-SHA256
  checksum live once in `api/utils/network_validator.py`; the web app's
  `fmtCoin` folded into `fmtCrypto`; the tests/ vs tests/api/ rule is written
  in both conftests.
* **B5** docs corrected against the code: the copy-paste examples in
  `docs/API.md` and `docs/EXAMPLES.md` no longer send `mainnet` or a Binance
  key; coin lists match the six purchase coins; `SETUP_GUIDE.md` migrations
  table runs to 025; `ROADMAP.md` and `GITHUB_ISSUES.md` no longer plan a
  Binance testnet run or list `/api/purchases` as live;
  `docs/PRODUCT_BACKLOG.md` carries a historical banner;
  `PRODUCTION_READINESS.md` states plainly that **SMS (Twilio) and push
  (FCM) do not work today** (the FCM code targets an endpoint Google retired
  in 2024), and its counts are current (25 migrations, 85 routes, TLS done on
  autobuy.minastrade.com via caddy).

## [Unreleased] — 2026-09-22: Batch A of the codebase audit (PR 1)

Deletions only; no behaviour change for the platform. Audit and plan:
tag `v0.6.5-pre-audit` is the last commit carrying everything below.

### Removed

* **The legacy sub-account auto-buy bot** (`autobuy.py`, `memebuy_cli.py`,
  `webapp/`, `memebuy/`, `chains/`, `payout/`, `safety/`,
  `docker-compose.legacy.yml`, the root `Dockerfile`, both
  `config*.example.json`, `requirements-legacy.txt`,
  `requirements-onchain.txt`, `deploy/bootstrap.sh`, `deploy/autobuy.service`,
  `docs/MEME_BUY.md`) and its ten test files. It was not running on the
  server (no legacy compose project, no container); a stopped bot in the
  repo is how someone redeploys it by accident. `scripts/deploy.sh` lost the
  `bot` and `all` targets and the bot-stack guard; `platform` is the default
  and only target. The four exchange-client modules and `exchange_errors.py`
  at the repo root stay: the platform imports them.
* `mobile/` (Expo scaffold): unreferenced by CI, scripts or the web app, and
  written against the retired `/api/purchases` routes.
* Web app: `purchaseAPI` (six methods calling routes that 404, zero call
  sites) and the legacy `Purchase`, `PurchaseCreate`, `PurchaseUpdate`,
  `Frequency` types; the mainnet/testnet validation generation
  (`addressVerdict`, `validateAddressForNetwork`, the stale `CRYPTO_CHAINS`
  mirror that still listed USDT, `EXCHANGE_NETWORKS`, `NETWORKS`,
  `canExchangeWithdrawToNetwork`); the unimported `SUPPORTED_CRYPTOS` and
  `FREQUENCIES` in `format.ts` that carried BNB/ADA/DOGE/LTC and a `biweekly`
  the API does not offer.
* API: `validate_address_for_network` and the mainnet/testnet half of
  `api/utils/exchange_networks.py` (only `CRYPTO_CHAINS` / `SUPPORTED_CRYPTOS`
  remain); the unread settings `supported_cryptos`, `binance_api_key`,
  `binance_api_secret` and their `.env.example` / compose lines; the log-only
  `POST /api/webhooks/resend` stub.
* Stragglers: `BINANCE_API_*` lines in `.env.production.example`, the stale
  `.env.example` comment, the bot-deploy sections of `deploy/AWS_EC2.md`,
  `deploy/ALIBABA_CLOUD.md` (entirely a bot guide), comments in
  `docker-compose.yml`, `deployment/nginx.web.conf`, `docs/DEPLOYMENT.md`,
  and the root `.dockerignore` (served only the deleted root `Dockerfile`;
  `Dockerfile.api` has its own ignore file, verified by building).
* `deployment/kubernetes.yaml` (self-labelled stub) and
  `deployment/nginx.conf` (nothing mounted it; the served config is
  `deployment/nginx.web.conf` behind caddy). Docs re-pointed.

### Tests removed, by name (backend 1,179 → see PR for the exact count)

`tests/test_memebuy.py` (99), `tests/test_safety_gate.py` (113),
`tests/test_solana_swap.py` (55), `tests/test_bitget_swap.py` (67),
`tests/test_payout.py` (63), `tests/test_bsc_swap.py` (49),
`tests/test_basket.py` (36), `tests/test_webapp.py` (25, already excluded
from CI), `tests/test_autobuy.py` (20), `tests/test_multi_exchange.py` (16),
`tests/test_exchange_networks.py` (8; its one live assertion, the purchase
coin list, moved to `tests/test_network_validator.py`),
`TestAddressForNetwork` in `tests/test_network_validator.py` (4),
`test_resend_webhook_stub` in `tests/api/test_coverage_gaps.py` (1).
Frontend: "address must match network and coin" and "exchange network
support" in `addressValidation.test.ts` (2).

Two of the removed bot tests exercised the kept `bitget_client.py` (request
signing, error-envelope handling); they were restored as
`tests/test_bitget_client.py`. Every other removed test imported only deleted
code (verified from each file's imports at the tag; none imported `api.*`).

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
