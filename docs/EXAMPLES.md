# Real-World Scenarios

## 1 — $100 of Bitcoin every week

Maria registers, passes KYC, adds a Bitget key, verifies it, and creates one
Auto-Buy purchase that sends each buy to her own wallet:

```json
POST /api/v2/purchases  {"crypto_symbol": "BTC", "amount_usd": 100, "frequency": "weekly",
                         "purchase_type": "autobuy", "wallet_address": "bc1q…",
                         "blockchain_network": "BTC", "exchange_account_id": "<key id>"}
```

Every week the executor buys $100 of BTC on her Bitget account, withdraws it
to her wallet, records the attempt (quantity, fill price, order id, withdrawal
id, fees, tx hash) and emails her a receipt. If her USDT balance runs dry the
attempt records `failed: …`, she gets an email saying so, and the purchase is
retried on the next 5-minute run, up to three attempts — no spam, no double
buys. Her dashboard shows cost basis and buy history; the tax CSV
(`/api/reports/tax-summary?format=csv`) lists every lot for tax season.

## 2 — Ten-coin portfolio

Sam runs six Auto-Buy purchases in accumulate mode (`purchase_type: "dca"`,
coins stay on the exchange):

- Majors weekly: BTC $200, ETH $150
- Alts weekly: SOL/XRP/XLM $50 each
- USDC monthly $25 (the six purchase coins are BTC, ETH, USDC, SOL, XRP, XLM)

`GET /api/dashboard/` aggregates all holdings with per-coin totals;
`/api/dashboard/analytics/timeline?days=90` shows his investment cadence.
Pausing the alts during a drawdown is one call each:
`PUT /api/v2/purchases/{id} {"status": "paused"}`. History survives even if
he cancels a purchase outright.

## 3 — Automated rebalancing (Phase 4+)

Planned: target-allocation portfolios ("60% BTC / 30% ETH / 10% SOL") where
the scheduler skews each cycle's buys toward underweight assets — dollar-cost
averaging that self-corrects drift without ever selling. The analytics
endpoints (performance, share of invested cost) added in Sprint 4 are the data
foundation for it.
