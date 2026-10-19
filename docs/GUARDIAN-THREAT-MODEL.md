# Guardian — threat model

Premise: **a breach of Guardian must not enable theft.** Adapted from
Guardian's standalone threat model for its life inside the platform, where it
shares a host, a database and a login with AutoBuy.

## Assets

| Asset | Sensitivity | Why |
|---|---|---|
| Customer read-only monitoring keys (`guardian_accounts`) | High | Read access to balances and history; not funds movement (verified read-only at onboarding, fail closed) |
| Customer purchase keys (`api_keys`) | Critical | Can trade; AutoBuy's asset, never read by Guardian code |
| `ENCRYPTION_KEY` | Critical | Decrypts every stored credential of both halves |
| `guardian_events`, balance history | High | Detailed financial activity per customer — privacy, not theft |
| Alert channel credentials (Telegram bot, Resend, ClickSend/Twilio) | Medium | Impersonation of our alerts; alert suppression |
| The detection pipeline | High | Silently disabled, customers believe they are protected when they are not |
| Platform sessions (JWT, refresh cookie) | High | A session is a customer's whole account: purchases and monitoring |

## Adversaries and scenarios

### A1. Full compromise of the host
Root on the box yields encrypted credentials and the process environment,
hence `ENCRYPTION_KEY`, hence every key. **The impact ceiling for monitoring
keys is surveillance, not theft**: they are read-only, verified fail-closed.
The purchase keys are AutoBuy's exposure and are governed by its own
controls (`CLAUDE.md`). Controls: non-root containers, no inbound ports but
443 and SSH, loopback-only API, the worker publishes nothing. Response:
rotate `ENCRYPTION_KEY` (`scripts/rotate_encryption_key.py`), tell customers
to rotate exchange keys, forensics from the append-only `guardian_events`.

### A2. Compromise of a customer's exchange account (the case we exist for)
Covered by the detection catalogue. The attacker's cleanup step — revoking
our monitoring key — is itself a HIGH detection
(`monitoring_key_tamper_suspected`), not a blind spot.

### A3. Alert-channel compromise
A leaked bot token or SMS credential lets an attacker send fake alerts or
learn which accounts are monitored. Controls: credentials only in `.env`;
alerts never carry keys or full emails; the customer copy says "sign in and
look", never "click this link". Gap, accepted: alerts are not signed.

### A4. Account takeover through the platform login
New with the merge: a stolen platform session reaches both purchases and
monitoring. Controls: optional TOTP for every account and mandatory for
admins (`ADMIN_MFA_REQUIRED`), sign out everywhere, the password-change and
two-factor-change tripwires written as events on every monitored account
(PR4), and the rule that changing the account email needs the password and
notifies the old address (PR3) — redirecting the alerts is what somebody does
before moving the funds.

### A5. Malicious or compromised insider
Admins can read event history and, with the key, credentials. Controls: the
append-only event log, every admin action audit-logged, admin 2FA. Role
separation beyond `admin` is organisational at this scale.

### A6. Supply chain
`ccxt` is the largest new dependency and sits on the credential path. It is
pinned to the exact version the four adapters were verified against; image
rebuilds are deliberate deploys, never auto-updates. Recommended next step:
an egress allowlist so the worker can only reach the exchanges and the alert
providers.

### A7. Guardian as an oracle
Someone onboards a victim's stolen read-only key to surveil them. Controls:
self-serve onboarding is per signed-in platform user and rate-limited; the
key's exchange uid is recorded; KYC-matching the key owner to the customer is
a business-process control the admin onboarding path keeps.

## Design properties

1. Read-only keys, verified programmatically, fail closed — the load-bearing wall.
2. One cipher for every secret at rest, decrypted only in worker memory per
   call, redacted from every error path.
3. Append-only `guardian_events`; the application has no UPDATE or DELETE
   path against a row's contents (retention deletes whole rows past their window).
4. Guardian code cannot reach an order, withdrawal or transfer endpoint and
   never reads a purchase key (`tests/test_guardian_isolation.py`).
5. Deterministic detection: every alert traces to a rule and raw evidence.

## Accepted residual risks

- `ENCRYPTION_KEY` in process memory: a root attacker wins.
- Unsigned alerts (A3).
- Admin role is binary; no finer RBAC.
- The worker shares the Tokyo egress IP with the trading poller: exchange
  rate limits are shared, and customers' IP-allowlisted keys must list it.
