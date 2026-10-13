# Working rules for this repository

AutoBuy buys crypto on a schedule on the customer's own Bitget sub-account.
Production is live with real customer money. These rules apply to every
session that works in this repository; `docs/deploy-log.md` holds the deploy
history and the current state of the server.

## Money and live trading

- `AUTOBUY_DRY_RUN` is never changed by an assistant. Anything that touches
  live trading (a real buy, a real withdrawal, re-arming a parked purchase,
  changing the executor, poller or withdrawal code) needs the owner's explicit
  say-so for that specific action.
- Never retry a buy without reconciling first: look up the order on the
  exchange and the `purchase_transactions` row before placing anything again.
  A duplicate buy is real money gone.
- A failed withdrawal never triggers a buy, and a failed buy never triggers a
  withdrawal. The two halves of a withdraw-mode purchase are separate steps.
- Money paths are off limits in audits and refactors: the fill guard,
  reconciliation, locks, idempotency keys, memo and tag constraints, the
  withdrawal fee mode. "Behaviour must not change" means the tests prove it.
- The Bitget withdrawal `tag` request field stays flagged unverified until a
  real tagged withdrawal has been seen.
- No order endpoint is ever called from a read-only job, a proof script or a
  test against a real account.

## Test accounts and data

- Never use a real customer's account for a check. Throwaway accounts are
  `support+test-YYYYMMDD@minastech.ca` style on the server and `*@example.com`
  locally; they are created for the check and deleted afterwards, with proof
  (a count of remaining rows) in the report.
- Nothing is sent to a real customer by a test: no email, no Telegram line,
  no purchase row that the scheduler could pick up.
- Holdings and the row digest of the live tables are read before and after
  every deploy and must be identical.
- Never print keys, secrets, passphrases or tokens: not from `.env`, not from
  the database, not from logs. Presence checks only ("set" / "unset").
- Customer emails are masked in anything that leaves the system (Telegram,
  logs, reports).

## Database

- Never edit an existing migration. A new migration gets the next number and
  is shown to the owner (upgrade and downgrade) before it is written into the
  tree, and before anything else is built on it.
- Dump before a destructive migration or any deploy:
  `pg_dump` to `~/backups/pre-<name>-<UTC timestamp>.sql.gz` on the server,
  listed and sized in the report.
- Query the server database read-only, through `psql` fed over stdin. The
  owner runs any query that decides what happens to a customer's purchase.
- Migrations run from the API container entrypoint on deploy; `alembic
  current` is part of the before/after check.

## Deployment

- Never edit `.env`, on the server or locally. New settings are given to the
  owner as exact lines, and every new setting goes in both `.env.example` and
  the `environment:` block of `docker-compose.yml`.
- Deploy on the server only, from `~/autobuy`, with `scripts/deploy.sh`.
  Running the script on the laptop stops at the environment guard and rewrites
  the local git origin.
- No deploy without the owner's go. Before a deploy: no purchase due in the
  next 30 minutes (check `next_execution` of every active purchase), the
  pre-deploy dump, the baseline (health commit, alembic head, dry-run flag,
  holdings, row digest). After: health commit, served bundle, alembic,
  holdings and digest unchanged, every scheduler job registered (seven from v0.7.7), both the
  purchase and withdrawal jobs run once with status ok, an unfiltered
  WARNING/ERROR scan of the log since the restart.
- `docker compose config` output is always produced with `--no-interpolate`.
  Interpolated output prints `.env` values.
- Never connect to 18.179.57.78. The server is the Elastic IP 16.76.92.209
  only (`scripts/lib/server_host.sh`).
- The web bundle is built on the laptop and shipped with `--web`; the deploy
  refuses a bundle that does not match the checkout.

## Working style

- Audit first. Report what exists, propose a plan, and stop for the owner's
  approval before writing code. The same for any schema change.
- Work on a branch and open a pull request; never commit to `main`. Separate
  commits per concern, separate PRs per part. Both test suites (backend and
  frontend) green locally and in CI before a PR is reported done, with test
  counts before and after.
- Report honestly what was verified in a browser or against the running
  system versus only unit-tested, and say which. Do not invent claims.
- Flag anything noticed in passing that risks money or data, in the report,
  even when it is outside the task.
- Rule files (`CLAUDE.md`, `docs/deploy-log.md`) are committed on their own,
  before feature work.
- User-facing errors are plain language: what happened and what to do, no
  codes, no apology.
- Amounts in the UI: at most 8 decimals for crypto, 2 for USDT. Times shown
  to customers that come from the scheduler are UTC and say so.
- Always `git fetch` before assuming the remote is quiet: a teammate commits
  from their own session.
