# Deploy log

Production is one Tokyo EC2 host. Real customer money has been on it since
2026-09-16 (`AUTOBUY_DRY_RUN=0`). This file is the record of what went live,
when, and with which backup, plus the rules and lessons that came out of it.
`CLAUDE.md` has the working rules; `docs/DEPLOYMENT.md` the long-form guide.

## Server

| | |
|---|---|
| Host | Elastic IP `16.76.92.209` (`scripts/lib/server_host.sh`). The old `18.179.57.78` was released on 2026-09-21 and is never used. |
| SSH | `ssh -i ~/.ssh/tokyo.pem ubuntu@16.76.92.209` |
| Checkout | `~/autobuy`, branch `main` |
| Stack | compose project `autobuy-platform`, prod profile: `autobuy_postgres`, `autobuy_redis`, `autobuy_api` (127.0.0.1:8000), `autobuy_web` (nginx on 127.0.0.1:8080, serving the bind mount `~/autobuy/frontend/dist`), `caddy` (public HTTPS) |
| Public URL | https://autobuy.minastrade.com |
| Database | `sudo docker exec -i autobuy_postgres psql -U autobuy -d autobuy_db -f -` with the SQL on stdin (read-only unless the owner says otherwise) |
| Backups | `~/backups/` on the server |
| Deploy command | on the server: `cd ~/autobuy && bash scripts/deploy.sh platform --branch main --web ~/web-<name>` |

The web bundle has no node on the server: build on the laptop
(`cd frontend && npm run build`), copy `dist` to `~/web-<name>` on the server
with scp, and pass it with `--web`. The deploy refuses a bundle whose
`BUILD_INFO` does not match the checkout.

## Standing deploy rules

1. No deploy without the owner's go, given for that deploy.
2. Before: no active purchase with `next_execution` in the next 30 minutes.
3. Before: `pg_dump` to `~/backups/pre-<name>-<UTC>.sql.gz`, listed with its size.
4. Before: baseline on the server with the scratchpad `server_pre.py` (health
   commit, alembic head, dry-run flag, holdings per coin, transaction row
   count and digest), run as `sudo docker exec -i autobuy_api python - < file`.
5. New settings go in the server `.env` by the owner before the deploy; the
   assistant gives the exact lines and never edits `.env`.
6. Deploy on the server only, with the command above. The script keeps the
   previous image tagged for rollback and fast-forwards git only.
7. After: health commit, `BUILD_INFO` via nginx (:8080) and via the public
   host, alembic head, holdings and digest identical to the baseline, the scheduler jobs registered (`execute_purchases`, `poll_withdrawals`,
   `drain_email_outbox`, `admin_alert_digest`, `weekly_digest`, and from
   v0.7.4 `low_balance_check`), the
   purchase job (every 5 min) and the withdrawal poll (every 10 min) each run
   once with status ok, an unfiltered WARNING/ERROR/Traceback scan of the API
   log since the restart.
8. Any browser check against the live site uses a throwaway account that is
   deleted afterwards with proof, and signs out at the end.
9. `docker compose config` is only ever printed with `--no-interpolate`.

## Deploys

Times are UTC. "Dump" is the file in `~/backups/` taken just before the
deploy. Every deploy below was verified with holdings and the row digest
identical before and after, alembic at `025` throughout, and both jobs
running ok, unless a note says otherwise.

| Date | Commit | PR | What | Dump | Web dir |
|---|---|---|---|---|---|
| 2026-09-22 09:47 | `fe1993d` | #36 | Audit batch A | `autobuy_db-before-batch-a-20260922T094708Z.dump` | `~/web-batch-a` |
| 2026-09-22 13:13 | `ea781d3` | #37 | Audit batch B | `autobuy_db-before-batch-b-20260922T131100Z.dump` | `~/web-batch-b` |
| 2026-09-22 14:15 | `f8467f6` | #38 | Audit B3, executor split | `autobuy_db-before-batch-b3-20260922T141337Z.dump` | `~/web-batch-b3` |
| 2026-09-22 16:24 | `0c48ddd` | #41 | Audit C1 + C2 | `autobuy_db-before-batch-c-20260922T162214Z.dump` | `~/web-batch-c` |
| 2026-09-22 19:27 | `97759aa` | #42 | Dashboard charts (tag `v0.7.0`) | `pre-charts-20260922T192446Z.sql.gz` | `~/web-charts` |
| 2026-09-22 21:06 | `471a0ae` | #43 | Changelog for charts | `pre-v070-20260922T210502Z.sql.gz` | |
| 2026-09-23 15:28 | `a5cbd5f` | #44 | Settings anti-autofill, direct-load crash fix | `pre-v071-20260923T152650Z.sql.gz` | |
| 2026-09-25 16:07 | `1fd7a6c` | #45 | Brand logo and favicon | `pre-v072-20260925T160451Z.sql.gz` | |
| 2026-09-25 16:52 | `7717f25` | #46 | Brand palette, link previews (tag `v0.7.1`) | `pre-v073-20260925T164937Z.sql.gz` | |
| 2026-09-28 03:04 | `902533f` | #47 | Telegram alerts | `pre-v074-20260928T030314Z.sql.gz` | |
| 2026-09-28 18:50 | `52ab2ab` | #48 | Landing page, invite-code signup gate, public meta | `pre-v075-20260928T184841Z.sql.gz` | |
| 2026-09-28 19:52 | `ff3058a` | #49 | Refresh 429 no longer signs the user out | `pre-v076-20260928T194549Z.sql.gz` | |
| 2026-10-12 12:40 | `56881e3` | #89 | **v0.8.1**: Unified definite refusals — 25204 maps to an honest not-found in the v3 client; definite buy refusals (25202 insufficient balance, 25207/25611 below minimum, 25100/25101/25102 pair not tradeable, 25013/25620 permission, plus the pre-send SYMBOL/MIN_NOTIONAL checks) fail outright with the Classic-equivalent outcome and emails, never needs_review; unknown errors keep the lookup path with one 5 s re-check on "not found". No migration (alembic 028), no `.env` lines, frontend unchanged (tree `a96b11c`, no `--web`). Verified: health at 56881e3, BUILD_INFO via nginx and public, seven jobs on four workers, purchase and withdrawal jobs ok (12:50:58), unfiltered log scan clean (only the known RequestsDependencyWarning), token absent, rows and digest identical (36 rows `90e8ebd1…`), v3 endpoint probe PASS through the deployed client, and the deployed lookup on the parked clientOid answers None. Then, on the owner's go: parked PEPE purchase `d7d68c79` resolved by admin mark_not_sent (Bitget confirmed no order: order-info 25204, v3 history empty since 10 Oct 12:06 UTC; attempt `48121622` → failed/not_found, audited) and the scheduler's 12:55:58 tick bought it live through the normal path: Bitget order 1493523866337431552, 479,616 PEPE at 0.00000417, 1.99999872 USDT, fee 479.616 PEPE; purchase completed; holdings net of fee 479,136.384 PEPE = Bitget's balance; USDT 11.0030275 → 9.00302878; one Telegram line (12:55:59, token redacted), customer email sent (Resend `01a1302f-2b3e-79ea-9e0d-3f68201ae03f`, 12:56:00, support account). Follow-ups filed: #90 plain-English failure text, #91 Funding-balance hint. | `pre-v0.8.1-20261012T124013Z.sql.gz` (16543 B) | unchanged (`~/autobuy/frontend/dist`, tree `a96b11c`) |
| 2026-10-10 11:43 | `fd6f870` | #85 | **v0.8.0.1**: Unified order read-back path `order-info` (was `order-details`, 40404); read-only v3 endpoint probe in the deploy smoke and the dry run. No migration (alembic 028), no `.env` lines. Pre-deploy: endpoint check on the SHIB key with raw GETs passed (account/info ok, assets ok, order-info → 25204 'Order does not exist', fills → 25200 time-range error; no 40404). Verified: health and BUILD_INFO at fd6f870, seven jobs on four workers, both jobs ok, log scan clean, token absent, Redis untouched, digest identical before the fills were recorded (34 rows). Post-deploy probe through the deployed client: PASS. Then admin record-fill on the two parked one-time SHIB purchases of the live test: `bb1638e0` → Bitget order 1492771371466117124 (11:05:49), `5c23712b` → 1492772630742978560 (11:10:50); each 917,767.9882 SHIB at 0.000005448, 4.9999999997 USDT, fee 917.7679882 SHIB, net 916,850.2202; both completed; holdings SHIB 1,833,700.4404 = Bitget's balance; zero new orders at any point. Customer emails still held. | `pre-v0.8.0.1-20261010T114305Z.sql.gz` (15869 B) | `~/web-v0.8.0.1` |
| 2026-10-10 09:40 | `d686e46` | #81 | **v0.8.0**: Unified (v3) support alongside Classic; **migration 028** `api_keys.account_mode` at boot (027 → 028), all six Bitget keys `unknown`, verified/status untouched by the migration; executor switch on 40085 only (approved). Pre-migration dump size-checked before the restart. Verified: health and BUILD_INFO at d686e46, seven jobs on four workers, both jobs ok, unfiltered log scan clean, token absent, Redis untouched, rows and digest identical (32 rows). Stub smokes (no Bitget call): Classic dummy key → classic; 40085-then-v3 with Trade+Management → unified, verified; Trade only → refused with the 'Almost there' message; executor dry run switched the key on 40085 and retried the same client order id through v3; the deployed Classic client makes the same five v2 calls and carries no v3 path. Real read-only re-verification of the three Classic keys: all `ok`, mode classic, `stow`. Owner's SHIB key re-verified through v3: mode unified, permissions `uta_trade` only → refused with the Management → Read message (the box was not yet reflected by Bitget); dry run therefore not run; no order placed. Customer emails held (`reverify_unified` without `--send-emails`). Rollback = `alembic downgrade 027`, redeploy v0.7.7.2 with `~/web-v0.7.7.2`. | `pre-v0.8.0-20261010T094029Z.sql.gz` (15429 B) | `~/web-v0.8.0` |
| 2026-10-08 22:37 | `45992c2` | #82 | **v0.7.7.2**: create and resume refused on an unverified or flagged key with the key's own message (`409 exchange_key_unverified`); disabled Resume in the list. No migration (alembic 027), no `.env` lines, executor untouched. Verified: health and BUILD_INFO at 45992c2, seven jobs on four workers, both jobs ok, log scan clean, token absent, Redis untouched, digest identical (32 rows). Smoke (throwaway): create on a flagged key → 409 with the Unified message, resume on it → 409, create on a verified dummy key → 201 (row paused and torn down); counts zero. Same day, before this deploy: the daily Unified check was run by hand at 22:04 UTC on the owner's instruction (flagged 2 keys, 2 emails, 2 Telegram lines, nothing to pause); a second parked SHIB purchase of the owner's (`5431348e`, attempted 22:01, before the check) was cancelled by admin resolve. | `pre-v0.7.7.2-20261008T223657Z.sql.gz` (15426 B) | `~/web-v0.7.7.2` |
| 2026-10-08 17:51 | `73a8b45` | #79 | **v0.7.7.1**: the daily check pauses a flagged key's active purchases through the normal pause path (status only) and the email says so. No migration (alembic 027), no `.env` lines, executor untouched, frontend unchanged. Verified: health and BUILD_INFO at 73a8b45, seven jobs on four workers, both jobs ok, log scan clean, token absent, Redis untouched, digest identical (31 rows). Smoke (throwaway with a verified dummy key and two active purchases; keyed stub venue answering 40085 only for the dummy key, a plain ok for every real key, so no Bitget call and no change to real keys): one key flagged, two purchases paused, one email queued, verified keys 6 → 5; teardown counts zero. | `pre-v0.7.7.1-20261008T175055Z.sql.gz` (14410 B) | `~/web-v0.7.7.1` |
| 2026-10-08 16:20 | `ca5e849` | #77 | **v0.7.7**: Unified Trading accounts refused at verification (40085 → `unified_account`); daily read-only `unified_account_check` (cron 06:20 UTC, **seventh job**) with one email + one Telegram line per key; purchase rows carry `exchange_key_error` for the banner; Settings line. No migration (alembic 027), no `.env` lines, executor untouched. Pre-deploy dump size-checked. Verified: health and BUILD_INFO at ca5e849, seven jobs on four workers, purchase and withdrawal jobs ok, unfiltered log scan clean, token absent, Redis untouched, rows and digest identical (31 rows). Smoke (throwaway, stub venue, no Bitget call): 40085 → `unified_account` with the exact message and a failed row; `stow` → verified; teardown counts zero. The first real pass runs 2026-10-09 06:20 UTC and will flag the two keys already on Unified accounts (owner's SHIB key, one customer key) with one email and one Telegram line each. Earlier the same day: parked SHIB purchase `b6b26bf1` cancelled by admin resolve with a note; customer purchase `b8e64e66` paused through the normal update path, anchor kept. | `pre-v0.7.7-20261008T162016Z.sql.gz` (14410 B) | `~/web-v0.7.7` |
| 2026-10-07 22:30 | `c36e695` | #68 | **v0.7.5.1**: schedule anchor, no more drift (executor change, approved). **Migration 027** at boot (alembic 026 → 027): `purchases.schedule_anchor` backfilled, 0 nulls; every active and paused row's next run identical before and after and its anchor equal to the pre-deploy next run (4 of 4), the 20 other rows anchored on their creation time. Also: Bitget icons for XLM, SHIB, PEPE, BONK, credits page lists ten logos with the trademark line. Pre-merge: branch already level with main (0 commits behind), spec and SDKs unchanged, CI green. Pre-migration dump size-checked before the restart. Verified: health and BUILD_INFO at c36e695, six jobs on four workers, purchase and withdrawal jobs ok, unfiltered log scan clean, token absent, Redis untouched, rows and digest identical (30 rows `7ce57f96…`). Live drift check on a throwaway (next-run maths only, no order, no Bitget call, 0 transaction rows created): a run 4 min late lands on anchor + 7 d, the next on anchor + 14 d, 16 idle days on anchor + 21 d; torn down with all counts zero. Credits page: ten logos loaded, no initial line, trademark line, 21 image requests all same-origin. Read-only verified keys still **1** (key `d4d2f70a…`, last verified 2026-10-06 05:39 UTC, no verify call since): the re-verify has not happened in the app. | `pre-v0.7.5.1-20261007T223030Z.sql.gz` (13951 B) | `~/web-v0.7.5.1` |
| 2026-10-07 21:23 | `2dea29b` | #74 | **v0.7.6**: coin logos (frontend only; backend byte-identical to v0.7.5). Six logos from brand sources served from `/logos/` (BTC, ETH, SOL, XRP, USDC, DOGE), lettered circles in the coin colour for XLM, SHIB, PEPE, BONK, `/logo-credits` generated from `LOGO_SOURCES.md`, footer link. No migration (alembic 026), no `.env` lines. Deploy launched detached; bundle checksums `ad3f789a…` matched. Verified: health and BUILD_INFO (nginx + public) at 2dea29b, a logo and the credits route served publicly, six jobs on four workers, purchase and withdrawal jobs ok, unfiltered log scan clean, token absent, Redis untouched, rows and digest identical (30 rows `7ce57f96…`). Smoke (throwaway, no purchase, no Bitget call): landing shows six logos and four circles, every logo loaded from `/logos/`; credits page lists ten rows with the ETH credit; picker at 1280 and 360 px shows DOGE's logo beside three circles in the coin colours at 32 px, main tab four logos and the XLM circle; no image loaded from outside our domain; teardown counts zero. | `pre-v0.7.6-20261007T212309Z.sql.gz` (13950 B) | `~/web-v0.7.6` |
| 2026-10-07 18:07 | `4228170` | #60 + #61 | **v0.7.5**: meme coins DOGE, SHIB, PEPE, BONK as accumulate-only; coin categories (main / stable / meme) in the picker, the landing page, market and public meta; withdraw gate refuses meme coins on every path, allowlisted owner included; number formatters (humans: separators, at least 4 significant digits for prices; CSV plain); low-balance email and Telegram line through the money formatter. No migration (alembic 026), no `.env` lines, executor untouched. Pre-merge: #60 conflicts in CHANGELOG, docs/API.md and the generated clients (both sides kept, clients regenerated: 9 schema additions); #61 one conflict in PurchaseList.test.tsx (both tests kept); BONK's colour `#334155` was missing from the asset colour map and was added. Deploy launched detached; bundle checksums `9c69c7ed…` matched. Verified: health and BUILD_INFO (nginx + public) at 4228170, six jobs on four workers, purchase and withdrawal jobs ok, public meta lists the three categories, unfiltered log scan clean, token absent, Redis untouched, rows and digest identical (30 rows `7ce57f96…`). Smoke (throwaway, no purchase created, no Bitget call): landing shows the meme group; at 1280 and 360 px the category tabs show, PEPE shows the risk line word for word and hides the withdraw toggle and wallet field, BTC shows no risk line; teardown counts zero. Owner's own one-time meme buy to follow. | `pre-v0.7.5-20261007T180701Z.sql.gz` (13951 B) | `~/web-v0.7.5` |
| 2026-10-07 13:32 | `111596c` | #70 | **v0.7.4.1**: Read-only Bitget keys (`stor`) refused at verification with a fix-it message; every sent email leaves a `notifications` row; low-balance pass and digest jobs once per tick (Redis `SET NX`, 60 s). No migration (alembic 026), no `.env` lines, executor untouched. Pre-merge: main unchanged since the branch was cut, no conflicts; spec and SDKs regenerate identically. Deploy launched detached after a dropped SSH connection (partial bundle folder removed, bundle re-copied with matching checksums `b2b607db…`). Verified: health and BUILD_INFO (nginx + public) at 111596c, six jobs on each of four workers, purchase and withdrawal jobs ok, unfiltered log scan clean, token absent, Redis untouched, rows and digest identical (30 rows `7ce57f96…`). Additions: no active purchase within 30 min (three XRP, 12–13 Oct; the USDC rows are completed or stopped); stub-venue smoke in-container with no Bitget call: `stor` → `read_only_key` with the exact message and a failed row, `stow` → verified; throwaway low-balance case sent exactly one email and left exactly **one** notifications row (sent), torn down with all counts zero; first low-balance tick at 14:02 logged **one** pass (was two or three). Verified keys still recording read-only codes after deploy: **1** (the tester's record; re-verify pending). | `pre-v0.7.4.1-20261007T133153Z.sql.gz` (13953 B) | `~/web-v0.7.4.1` |
| 2026-10-06 13:07 | `1a4d119` | #55 | **v0.7.4**: pre-buy low-balance warning. **Migration 026** applied at boot (two nullable columns on purchases, 17 rows, 64 kB table), alembic 025 → 026; **six** scheduler jobs with `low_balance_check` first run at 13:37 UTC (nothing in window); no new `.env` lines. Verified: health and BUILD_INFO (nginx + public) at 1a4d119, holdings and digest identical (21 rows), purchase and withdrawal jobs ok, log scan clean, token absent, Redis untouched. Smoke: preference visible and on, dashboard and purchase row without a banner; a throwaway with an injected balance reader got exactly one email (Resend id in the log, drained within the minute), both banners appeared, a top-up cleared them with nothing sent; torn down with all counts zero. Gap noted: the sent email left no `notifications` row. | `pre-v0.7.4-20261006T130724Z.sql.gz` (11622 B) | `~/web-v0.7.4` |
| 2026-10-06 11:07 | `b06e2c0` | #65 | **v0.7.3.1**: sign-out from a stale tab revokes for real (issue #64). No migration, alembic 025, no new `.env` lines. Verified: health and BUILD_INFO (nginx + public) at b06e2c0, holdings and digest identical (21 rows), five jobs, purchase and withdrawal jobs ok at 11:17 UTC, log scan clean apart from the smoke tests' own lines, token absent, Redis untouched (67 → 70 revocation keys). Smoke: a v0.7.3 session survived the deploy; two-tab reload 2/2; stale-tab sign-out 204 and the other tab signed out on its next reload, 2/2; API logout with a stale header 204 and both cookie sets 401 afterwards; throwaway torn down, all counts zero. | `pre-v0.7.3.1-20261006T110642Z.sql.gz` (12068 B) | `~/web-v0.7.3.1` |
| 2026-10-05 13:45 | `c1190ea` | #57 | **v0.7.3**: refresh rotation grace window (refresh and CSRF cookies). No migration, alembic 025, no new `.env` lines. Verified: health and BUILD_INFO (nginx + public) at c1190ea, holdings and digest identical, five jobs, purchase and withdrawal jobs ok at 13:56 UTC, log scan clean apart from the smoke tests' own lines, token absent; Redis untouched (54 → 57 revocation keys, container not restarted); smoke: a session signed in on v0.7.2 survived the deploy, two-tab reload 2/2 rounds, logout refuses the previous and the current cookie set with 401; throwaway torn down, all counts zero. | `pre-v0.7.3-20261005T134458Z.sql.gz` (10285 B) | `~/web-v0.7.3` |
| 2026-10-01 18:11 | `c93f23c` | #50 #51 #52 #53 #54 | **v0.7.2**: private-beta withdraw gate (all paths), fee wording, landing follow-ups, rule files. New `.env`: `WITHDRAWALS_ENABLED=0`, `WITHDRAW_ALLOWED_EMAILS`. Verified: health and BUILD_INFO (nginx + public) at c93f23c, alembic 025, holdings and digest identical, five jobs, purchase and withdrawal jobs ok at 18:21 UTC, log scan clean, token absent; live gate check with a throwaway account (403 on create and resume, no Resume in the list, Settings beta line) torn down with all counts zero. | `pre-v0.7.2-20261001T181138Z.sql.gz` (9359 B) | `~/web-v0.7.2` |

Earlier reference points, before this log starts: `v0.6.0` (`f639ae8`,
2026-09-17, migrations 023 + 024, dump
`autobuy_db-before-023-024-20260917T142521Z.dump`, restore-rehearsed),
`v0.6.2` (dump `autobuy_db-before-v0.6.2-20260917T162342Z.dump`), `v0.6.4`
(dump `autobuy_db-before-v0.6.4-20260921T163219Z.dump`), migration 025 on
2026-09-21 (dump `autobuy_db-before-025-20260921T172747Z.dump`, BTC row
before/after files next to it), public HTTPS on 2026-09-21 (dump
`autobuy_db-before-public-https-20260921T200452Z.dump`).

## Rollback points

| Tag | Commit | Meaning |
|---|---|---|
| `v0.6.5-pre-audit` | `d4d69af` | Known-good point before the codebase audit batches |
| `v0.7.0` | `97759aa` | Dashboard charts |
| `v0.7.1` | `7717f25` | Release covering #44, #45, #46 |
| `v0.7.2` | `c93f23c` | Withdraw gate, fee wording, landing follow-ups, rule files (#50–#54) |
| `v0.7.3` | `c1190ea` | Refresh rotation grace window (#57) |
| `v0.7.3.1` | `b06e2c0` | Sign-out from a stale tab (#65) |
| `v0.7.4` | `1a4d119` | Low-balance warning, migration 026 (#55); rollback = `alembic downgrade 025` in the container, then redeploy v0.7.3.1 with `~/web-v0.7.3.1` |
| `v0.7.4.1` | `111596c` | Read-only keys refused, sent-row, tick guard (#70); no migration: rollback = redeploy v0.7.4 with `~/web-v0.7.4` |
| `v0.7.5` | `4228170` | Meme coins and categories (#60 + #61); no migration: rollback = redeploy v0.7.4.1 with `~/web-v0.7.4.1` |
| `v0.7.6` | `2dea29b` | Coin logos (#74); frontend only: rollback = redeploy v0.7.5 with `~/web-v0.7.5` |
| `v0.7.5.1` | `c36e695` | Schedule anchor, migration 027 (#68); rollback = `alembic downgrade 026` in the container, then redeploy v0.7.6 with `~/web-v0.7.6` |
| `v0.7.7` | `ca5e849` | Unified-account detection (#77); no migration: rollback = redeploy v0.7.5.1 with `~/web-v0.7.5.1` |
| `v0.7.7.1` | `73a8b45` | Flagged key pauses its purchases (#79); no migration: rollback = redeploy v0.7.7 with `~/web-v0.7.7` |
| `v0.7.7.2` | `45992c2` | No purchase on an unverified key (#82); no migration: rollback = redeploy v0.7.7.1 with `~/web-v0.7.7.1` |
| `v0.8.0` | `d686e46` | Unified support, migration 028 (#81); rollback = `alembic downgrade 027` in the container, then redeploy v0.7.7.2 with `~/web-v0.7.7.2` |
| `v0.8.0.1` | `fd6f870` | Order read-back path (#85); no migration: rollback = redeploy v0.8.0 with `~/web-v0.8.0` |

To roll the code back: on the server, create a branch at the tag
(`git branch rollback-v0.7.1 v0.7.1`) and deploy it with
`bash scripts/deploy.sh platform --branch rollback-v0.7.1 --web <matching web dir>`. The old web bundles stay in
`~/web-*`. The deploy script also keeps the previous API image tagged
`bitget-autobuy:rollback`, but that tag dates from 2026-09-15 and the
current image is `autobuy-api:latest`, so the git tag plus a rebuild is the
reliable path. Database rollback is a restore of the dump listed for that
deploy; no deploy in the table above changed the schema.

## Lessons recorded

- nginx bind-mounts `frontend/dist` by inode. Never `mv` a new dist into
  place; the deploy script installs in place. If `BUILD_INFO` is not served
  after a deploy, recreate the web container:
  `sudo docker compose -p autobuy-platform --profile prod up -d --force-recreate web`.
- A deploy without `--web` left an old bundle live once (v0.6.0). That is why
  the script now refuses to finish without a matching `BUILD_INFO`.
- The script's "compose profile does not include the api service" guard can
  fail transiently; rerun the same command.
- Running `scripts/deploy.sh` on the laptop stops at the environment guard and
  rewrites the local git origin to the SSH URL. Run it on the server only.
- `docker logs autobuy_api` covers only the current container and every
  deploy recreates it. Durable evidence lives in the `notifications` table and
  the Redis list `autobuy:email:undelivered`.
- The refresh endpoint is limited to 10 per minute per IP. A browser sweep
  that loads more than ten routes in a minute looks like lost sessions; split
  the sweep. The client no longer signs the user out on that 429 (PR #49).
- `psql -c` with two statements runs them in one transaction; use separate
  `-c` calls. Inline heredocs lose quotes through ssh: scp a file and feed it
  on stdin.
- A GitHub release's target shows the default branch, not the tag's commit.
  Check `git rev-parse <tag>^{}`.
- `GET /` via the public host can return 000 transiently from the laptop.
  Retry before concluding anything.
- The Telegram token must never appear in logs. The httpx line is redacted
  to `bot<redacted>` and every deploy since #47 checks for zero matches.
- Resend answered `daily_quota_exceeded` for the production account on
  2026-09-22; purchase emails dead-lettered. The quota may have reset since
  (a later validation error suggests so); the owner retests.
- The public fee figure from Bitget's symbols endpoint (0.2%) is not the
  account's real tier (fills at 0.1%). The UI no longer shows a figure.
- Server log scans must not filter out "email is not configured"; that line
  came from a local proof API, never from the server.

## How to add an admin

Roles live on `users.role`. On the server, read-only check first, then the
one-line update, both fed to psql on stdin:

```sql
select id, email, role from users where email = 'person@example.com';
update users set role = 'admin' where email = 'person@example.com';
```

`scripts/make_admin.py` does the same but fails from the host because
`DATABASE_URL` points at the compose hostname; inside the container it works
(`sudo docker exec -i autobuy_api python scripts/make_admin.py person@example.com`).
The owner account `support@minastech.ca` was promoted this way on 2026-09-22.
Every admin action is written to the audit table.

## Current known state (2026-10-12, after the v0.8.1 deploy)

- Server at `56881e3` = `v0.8.1`, alembic `028`, seven scheduler jobs, dry run off,
  schedulers not paused. `WITHDRAWALS_ENABLED=0` and `WITHDRAW_ALLOWED_EMAILS` are set; only
  the allowlisted owner can use withdraw mode.
- Three active purchases, all XRP weekly accumulate: the original (10 USD, next
  run **2026-10-12 00:16 UTC**), a second created on 2026-10-05 (next run
  **2026-10-12 18:16 UTC**) and a third created on 2026-10-06 (next run
  **2026-10-13 14:42 UTC**). The only withdraw-mode purchase in the table is
  the owner's own one-time BTC purchase from 2026-09-15, status failed.
- The 2026-10-10 PEPE park is closed: resolved mark_not_sent after Bitget
  confirmed no order, re-bought live 2026-10-12 12:55:58 (order
  1493523866337431552), purchase completed, holdings match Bitget. The support
  account now also holds 479,136.384 PEPE.
- Two active purchases, both XRP weekly: next runs 19 Oct 00:16:57 UTC (the
  12 Oct 00:16 run bought 7.0957 XRP at 1.4093, order 1493333215678996480)
  and 13 Oct 14:42:44 UTC (then 20 Oct 14:42:44). The
  customer purchase `b8e64e66` (next 12 Oct 18:16) is paused: its Bitget
  account turned Unified. Bitget migrates Classic accounts to Unified Trading
  mode in batches since 2026-09-15; two of five verified keys are Unified.
- The v0.8.0 live test passed after the path fix: two 5 USDT SHIB buys through v3
  on the owner's key, both recorded by admin record-fill, holdings match Bitget.
  The support account's SHIB key is Unified with Trade + Management → Read.
- Next: the held customer emails (`reverify_unified --send-emails`, owner's go);
  issue on blocking a key with an unresolved review; #71; the single-scheduler
  process.
- One verified key (`d4d2f70a…`) still records read-only codes; its owner has
  not re-verified in the app since 2026-10-06 05:39 UTC.
- The 2026-10-06 USDC stop (Bitget 40014) was a Read-only key; the tester
  switched it to Read-write. Verification refuses such keys since v0.7.4.1; one
  verified key still records read-only codes until the tester re-verifies.
- Owner-side: Resend quota retest, KYC and compliance decisions, legal text
  review.
- Backlog: issue #35 (order-dependent admin alert digest test), #39 (remove
  the purchase_scheduler shim), #40 (caddy access log); audit PR 5 (C3) waits
  for an explicit go.
- Not running on the server: the Prometheus/monitoring profile. Email and
  SMS to customers depend on Resend; FCM and Twilio are not set up.
