# Guardian — data retention (flagged for legal review; not yet reviewed)

Legal/compliance has not reviewed the monitoring offering (confirmed at
Guardian's kickoff, 2026-07-02; still true at the merge, 2026-10-13). This
document exists so engineering choices do not silently become policy; it
states the current practice accurately and lists the decisions a lawyer must
make. Nothing here is legal advice.

## Current practice

Every window comes from `guardian/rules.yaml` (`retention:`) or the defaults
in `guardian/rules_schema.py`, and is enforced every
`retention.prune_interval_minutes` by `prune_all` (`guardian/retention.py`)
inside the worker. The member status page (PR4) shows the same numbers,
derived from the same config, so the page and the practice cannot disagree.

| Data | Table(s) | Current retention |
|---|---|---|
| Forensic event log | `guardian_events` | 730 days |
| Alerts and triage notes | `guardian_alerts`, `guardian_alert_triage` | 730 days |
| Operator pages, SMS delivery records | `guardian_operator_pages`, `guardian_sms_deliveries` | 730 days |
| "That was me" marks, threshold changes, snoozes | `guardian_expected_activity`, `guardian_config_changes`, `guardian_snoozes` | 730 days |
| Dedup records | `guardian_seen_*` | 365 days — bounded below by correctness: shorter than the exchanges' history and a re-polled record re-alerts |
| Balance snapshots | `guardian_balance_snapshots` | 7 days, latest per account kept |
| Permission snapshots | `guardian_permission_snapshots` | 730 days, latest per account kept |
| Monitoring keys (encrypted) and contact overrides | `guardian_accounts` | until the account is disabled and the key scrubbed (PR3), or deleted with the user |
| Baselines (known addresses, traded pairs, transfer routes, fee EMA) | never pruned | deleting a baseline row invents an anomaly |
| Identity, 2FA | `users`, `user_totp*` | the platform's; outside this module |

Hosting is the platform's: AWS Tokyo (ap-northeast-1).

## Questions legal must answer before general availability

1. **Jurisdiction**: the company operates from Canada; hosting is AWS Japan.
   PIPEDA at minimum, possibly provincial rules plus APPI for the region.
2. **Retention period**: financial-activity logs often carry minimum
   (AML-adjacent) and maximum (privacy) obligations; 730 days is an engineering
   default, not an answer to either. A right-to-erasure request conflicts with
   the append-only log; crypto-shredding (a per-account key destroyed on
   erasure) is implementable if deletion-equivalence is wanted.
3. **Customer consent**: the monitoring agreement must cover what is collected
   and that staff can see activity data.
4. **Breach notification**: thresholds and timelines per jurisdiction if the
   event log is exposed.

## Engineering practice until legal says otherwise

- Pruning runs on schedule with the windows above; `0` means keep forever and
  is a legitimate per-table choice.
- Offboarding = `status='disabled'` plus scrubbing the key material; history
  is kept. Deleting a platform user cascades to their monitoring tables.
- Guardian's optional AI triage (third-party processing of event data) was not
  brought over.
