# Guardian — security

Guardian holds customer exchange API credentials, so the monitoring half of
the product is itself a sensitive asset. This states the invariants and how
secrets are handled inside the platform. It adapts Guardian's standalone
security policy; the platform's own rules in `CLAUDE.md` apply on top.

## The non-negotiable invariant

**Guardian only ever holds read-only API keys.** Onboarding verifies the
scope against each exchange's own key-metadata signal and rejects, fail
closed, any key with trade, withdraw or transfer permission:

| Exchange | Read-only signal | Accept only when |
|---|---|---|
| Bitget | `authorities` (per-module `…r` / `…w` codes) | every token is read-scope; any write, trade, withdraw or transfer token → reject |
| Bybit | `readOnly` flag + `permissions` | `readOnly == 1` **and** no write sub-permission |
| OKX | account-config `perm` | `perm == read_only` exactly |
| Binance | `apiRestrictions` flags | `enableReading` true **and** every write-implying flag false; a missing flag fails closed |

Any error, unparseable response or ambiguity is a rejection, never a pass.
The gate lives in one place (`guardian/onboarding.py`); the API's self-serve
and admin onboarding routes (PR3, PR5) call that same function and cannot
bypass it. The platform's purchase keys live in `api_keys` and must be able to
trade; a purchase key presented as a monitoring key is refused by its
fingerprint, and a monitoring key is never read by any trading code
(`tests/test_guardian_isolation.py`).

Known Bitget false rejections: Guardian's gate refuses any code ending in
`w`, which includes the read scopes `taxw` (Taxation → Search) and `p2pw`
(P2P → Search) that the platform's permission table documents as reads.
PR3 fixes this by sharing that table, still failing closed on unknown codes.

## Secret handling

- Credentials are **encrypted at rest** with the platform cipher
  (`api/services/crypto.py`: Fernet keyed by the sha256 of `ENCRYPTION_KEY`),
  the same cipher as the purchase keys and the TOTP secrets, rotated by
  `scripts/rotate_encryption_key.py`. `SENTINEL_MASTER_KEY` exists only as an
  input to the one-off cutover script.
- Decrypted values live in worker memory for the duration of one exchange
  call; `redact_secrets` strips them from anything that is logged or alerted.
- The API never returns a stored key; responses carry a masked form at most.
- Presence checks only in logs and reports: "set" / "unset", never a value.

## Network and access posture

- The worker publishes no port. Its Prometheus exposition is reachable on the
  compose network only.
- The container runs non-root on the API image with `cap_drop: [ALL]`,
  `no-new-privileges`, pid and memory limits.
- Every outbound socket from `guardian/` is `guardian/net.py`, which refuses
  to connect under test; email leaves through the platform's Resend outbox
  and Telegram through the platform's sender, which redacts the bot token
  from logs and masks customer emails.
- Admin pages that expose customers' contact details require an admin with
  two-factor authentication once `ADMIN_MFA_REQUIRED=1` (migration 029).

## What Guardian does not do

It detects and tells. It cannot freeze an account, cancel a withdrawal or
move funds, and its copy never claims otherwise
(`tests/guardian/test_no_prevention_claims.py`).

## Reporting a vulnerability

Private repository: report privately to the support address in `SUPPORT_EMAIL`,
never in a public issue. Treat any suspected key exposure as urgent and rotate
the affected keys first.
