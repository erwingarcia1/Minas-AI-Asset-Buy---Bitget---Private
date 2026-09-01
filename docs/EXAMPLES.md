# Real-World Scenarios

## 1 — $100 of Bitcoin every week

Maria registers, passes KYC, adds a spot-trade-only Binance key, verifies it,
and creates one schedule:

```json
POST /api/purchases/  {"crypto": "BTC", "amount_usdt": 100, "frequency": "weekly"}
```

Every week the scheduler buys $100 of BTC with her funds on her Binance
account, records the fill (`amount_crypto`, `exchange_rate`, order id), and
emails her a receipt. If her USDT balance runs dry, the transaction records
`failed: insufficient balance`, she gets an email saying so, and the schedule
retries an hour later — no spam, no double buys. Her dashboard shows cost
basis and buy history; `/api/transactions/stats/summary` gives her average
purchase price for tax season.

## 2 — Ten-coin portfolio

Sam runs ten schedules (one per coin — the API rejects duplicate active
schedules per coin, so the portfolio stays intentional):

- Majors weekly: BTC $200, ETH $150
- Alts biweekly: SOL/BNB/XRP/ADA $50 each
- Small monthly punts: DOGE/LTC $25 each

`GET /api/dashboard/` aggregates all holdings with per-coin totals;
`/api/dashboard/analytics/timeline?days=90` shows his investment cadence.
Pausing the alts during a drawdown is one call each:
`PATCH /api/purchases/{id}/toggle`. History survives even if he deletes a
schedule outright.

## 3 — Automated rebalancing (Phase 4+)

Planned: target-allocation portfolios ("60% BTC / 30% ETH / 10% SOL") where
the scheduler skews each cycle's buys toward underweight assets — dollar-cost
averaging that self-corrects drift without ever selling. The analytics
endpoints (performance, diversification) added in Sprint 4 are the data
foundation for it.
