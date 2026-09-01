# AutoBuy Crypto Platform — Roadmap

*Prepared 2026-09-01. Companion to [`GITHUB_ISSUES.md`](GITHUB_ISSUES.md) (24 issues, 167 story points).*

---

## Timeline (Gantt)

Assumes start Mon **2026-09-07**, one full-time backend dev + one frontend dev joining week 4 (see Capacity). Dates shift proportionally for other staffing.

```
                        Sep 07   Sep 14   Sep 21   Sep 28   Oct 05   Oct 12   Oct 19   Oct 26
                        W1       W2       W3       W4       W5       W6       W7       W8
Sprint 0 (foundation)
  Alembic + schema fix  ████
  Integration tests     ████████
PHASE 2 — Core Services
  Purchase executor         ████████
  Scheduler                     ████████
  KYC (Sumsub)                       ████████
  Key verification              ████
  Email (Resend)                         ████
  Validation/err handl.         ████
PHASE 3 — Testing/Stability
  Unit tests (services)                  ████████
  Security audit                             ████████
  E2E suite                                       ████
  Perf/load tests                                 ████
PHASE 4 — Production
  Monitoring/logging                                  ████
  Rate limiting                                       ██
  Deploy config + CI/CD                               ████████
  API docs                                                ████
PHASE 5 — Frontend (parallel from W4)
  Web dashboard                      ████████████████████████████
  Admin panel                                                ████████
────────────────────────────────────────────────────────────────────────────────
MILESTONES               M0       M1            M2       M3       M4      LAUNCH
```

Phase 6 (analytics, multi-exchange, webhooks, SDKs, reporting) is post-launch and unscheduled.

## Milestones

| # | Date (target) | Milestone | Exit criteria |
|---|---|---|---|
| M0 | Sep 11 | **Foundation locked** | Alembic baseline live, cascade-delete fixed, router integration tests in CI |
| M1 | Sep 25 | **First automated purchase** | Scheduler executes a testnet/dry-run buy end-to-end; transaction recorded |
| M2 | Oct 02 | **Compliance-ready** | Sumsub flow live in sandbox; self-serve KYC removed; email receipts sending |
| M3 | Oct 09 | **Hardened** | Security audit closed, ≥80 % service coverage, e2e green in CI |
| M4 | Oct 16 | **Deployable** | Prod compose profile + CI/CD + monitoring/alerts; API frozen & documented |
| 🚀 | **Oct 26–30** | **MVP launch** | Web dashboard live; first real (small) purchases with real users |

## Team capacity considerations

- **Current reality:** this repo has been effectively a one-developer operation. The 7–8-week plan above requires **1 FTE backend from W1** and **1 FTE frontend from W4**. With a single developer doing both, Phase 5 serializes behind Phase 4 → launch slips to **~13 weeks (early December)**.
- **Sequencing rule:** Sprint 0 (Alembic #12 + integration tests #8) is deliberately pulled ahead of its "Phase 4/3" labels — every Phase 2 issue adds columns or writes money-moving code, and doing that without migrations and a test net is how you corrupt production data.
- **Parallelism:** the frontend can start on auth/dashboard screens the moment Phase 2's API shape stabilizes (M1); the Sumsub widget needs M2. Admin panel waits for the role migration + audit log.
- **Don't split the executor:** #1, #2, #5 belong to one person, in this order; handing the scheduler and executor to different people multiplies the idempotency risk.
- **Review load:** money-touching code (#1, #2, #11) should get a second pair of eyes even if the second person is part-time; budget ~15 % reviewer time.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Real-money execution bug** (double-buy, wrong amount) | Medium | Critical | Idempotency per due-date (#1 AC), dry-run default, testnet first, small-amount canary launch, capped per-user amounts (#6 validation) |
| **Self-serve KYC reaches production** | Low | Critical (regulatory) | #3 removes user-writable `kyc_status` and gates the executor; M2 is a hard launch gate |
| **API key compromise** | Low | Critical | Fernet at rest today; #11 splits encryption key from JWT secret + rotation script; #5 warns on withdrawal-permission keys |
| **Cascade-delete destroys transaction history** | High (it's live behavior today) | High | Fixed in Sprint 0 (#12 `ON DELETE SET NULL`) before any real transactions exist |
| **Single-venue dependency (Binance outage/geo-blocks)** | Medium | Medium | Accept for MVP; legacy repo already has Bitget/Bybit/OKX clients → Phase 6 multi-exchange is cheap insurance |
| **Timeline slip on solo staffing** | High | Medium | Cut line is explicit: Phase 6 entirely, admin panel (#18), perf tests (#10) can all slip without blocking launch |
| **Scheduler double-execution across replicas** | Medium | High | Redis/advisory lock is an acceptance criterion of #2, not an afterthought; single-replica deploy until proven |
| **Sumsub/Resend integration surprises** | Medium | Medium | Both are sandbox-testable; webhook contract tests with recorded payloads; email is fire-and-forget so it can't block trades |
| **Secrets misconfiguration in prod** | Medium | High | #11: boot refusal on default secrets; deploy runbook (#16) includes a secrets checklist |

## Standing decisions (context for anyone joining)

1. **Reuse the legacy exchange clients.** `binance_client.py` (and siblings) already solve precision, min-notional, and normalized market buys. The platform wraps them; it does not rewrite them.
2. **The Flask `webapp/` is not the frontend.** It is the legacy bot's internal VA tool. The customer UI (#17) is a separate React app.
3. **Redis exists for #2's lock, #14's rate limits, and #20's price cache.** Nothing else should quietly adopt it without an issue.
4. **Naive-datetime columns are debt, not a pattern.** New columns are `DateTime(timezone=True)` from #12 onward; responses are already timezone-aware UTC.
