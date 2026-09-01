# Admin Guide

## Becoming an admin

Roles live on the `users.role` column and only existing admins (or the server
operator) can grant them:

```bash
python scripts/make_admin.py you@example.com
```

The web admin UI is at `/admin` (redirects non-admins away); the API surface is
`/api/admin/*` (403 without the role). Every state-changing admin action writes
an `admin_action` audit line to the JSON logs with the acting admin's id.

## What you can do

| Action | Where | Notes |
|---|---|---|
| Platform stats | Overview tab · `GET /api/admin/stats` | totals + 7-day trend inputs |
| Find users | Users tab · `GET /api/admin/users?q=` | email substring search, invested totals |
| Suspend / unsuspend | Users tab | suspension blocks API access **and** purchase execution immediately |
| Delete a user | Users tab | requires typing the exact email; cascades keys/schedules/history |
| KYC override | `POST /api/admin/users/{id}/kyc-override` | mandatory note (≥5 chars) — lands in the audit log |
| Transaction audit | Transactions tab | cross-user, filterable, CSV export |
| Force-retry a schedule | `POST /api/admin/purchases/{id}/force-retry` | makes it due now; next tick executes |
| Pause / resume scheduler | `POST /api/admin/scheduler/pause` / `resume` | emergency trading stop without a deploy |
| Live alerts | Alerts tab · `GET /api/admin/alerts` | evaluates the docs/ALERTING.md conditions in-app, refreshes every 30s |

## Alert response

Follow the runbook in [ALERTING.md](ALERTING.md). The fastest levers, in order:
pause the scheduler (stops all buying), suspend a single user (stops theirs),
`AUTOBUY_DRY_RUN=1` + restart (keeps scheduling, simulates orders).
