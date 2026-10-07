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
| **pending** | PR #68 head | #68 | **v0.7.4.2**: schedule anchor (migration 027, backfilled; the live XRP anchors stay at 00:16 and 18:16 UTC). Executor change: deploy only after both 12 Oct XRP buys completed; earliest Tuesday 13 Oct. | `pre-v0.7.4.1-<UTC>.sql.gz` | `~/web-v0.7.4.1` |
| **pending** | PR #55 head | #55 | **v0.7.4**: pre-buy low-balance warning. **Migration 026** (two nullable columns on purchases; dump before, rehearsed down/up locally); alembic 025 → 026; **six** scheduler jobs after the deploy (`low_balance_check` every 30 min); no new `.env` lines. Deploys only on the owner's word, 30-minute purchase check against the 12 Oct 00:16 UTC XRP run. | `pre-v0.7.4-<UTC>.sql.gz` | `~/web-v0.7.4` |
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

## Current known state (2026-10-06, after the v0.7.3.1 deploy)

- Server at `b06e2c0` = `v0.7.3.1`, alembic `025`, dry run off, schedulers not
  paused. `WITHDRAWALS_ENABLED=0` and `WITHDRAW_ALLOWED_EMAILS` are set; only
  the allowlisted owner can use withdraw mode.
- Two active purchases, both XRP weekly accumulate: the original (10 USD, 5 Oct
  buy filled at 00:16:57 UTC, next run **2026-10-12 00:16 UTC**) and a second
  one created on 2026-10-05 18:16 UTC (next run **2026-10-12 18:16 UTC**). The only withdraw-mode purchase in the table is
  the owner's own one-time BTC purchase from 2026-09-15, status failed.
- Next: `v0.7.4` = PR #55 (low-balance warning, migration 026, sixth scheduler
  job), on the owner's word; then `v0.7.5` = PR #60 + #61 (meme coins).
- Owner-side: Resend quota retest, KYC and compliance decisions, legal text
  review.
- Backlog: issue #35 (order-dependent admin alert digest test), #39 (remove
  the purchase_scheduler shim), #40 (caddy access log); audit PR 5 (C3) waits
  for an explicit go.
- Not running on the server: the Prometheus/monitoring profile. Email and
  SMS to customers depend on Resend; FCM and Twilio are not set up.
