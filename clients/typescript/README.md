# @autobuy/ts-client

Type-safe TypeScript client for the **AutoBuy Crypto API**. Types and resource
methods are generated from the API's OpenAPI spec (`clients/openapi.json`);
the HTTP layer handles bearer auth and transparent JWT refresh.

- Zero runtime dependencies — uses WHATWG `fetch` (Node ≥ 18, browsers, Deno, Bun)
- Full `async/await` API, strict TypeScript types for every request and response
- Automatic single-flight token refresh on 401, `onTokensChanged` hook for persistence
- Typed errors: `ApiError`, `AuthenticationError`, `RateLimitError` (with `retryAfter`), `ValidationError` (with field `errors`)

## Installation

```bash
npm install @autobuy/ts-client
# from this repo (until published):
npm install ./clients/typescript
```

## Usage

```ts
import { AutoBuyClient, ApiError, RateLimitError } from "@autobuy/ts-client";

const client = new AutoBuyClient({
  baseUrl: "http://localhost:8000",
  // persist tokens however you like (localStorage, keychain, ...)
  onTokensChanged: (tokens) => localStorage.setItem("autobuy", JSON.stringify(tokens)),
});

// 1. Register + login
await client.register("me@example.com", "Str0ng!Passw0rd");
await client.login("me@example.com", "Str0ng!Passw0rd");

// 2. Who am I?
const me = await client.me();            // UserProfile

// 3. Schedule a recurring buy (requires verified KYC + a verified exchange key)
const purchase = await client.autoBuy.createPurchase({
  crypto: "BTC",
  amount_usdt: 50,
  frequency: "weekly",                    // daily | weekly | biweekly | monthly
});

// 4. History + dashboard
const page  = await client.transactions.listTransactions({ limit: 20, offset: 0 });
const stats = await client.dashboard.quickStats();

// 5. Error handling
try {
  await client.autoBuy.getPurchase("does-not-exist");
} catch (e) {
  if (e instanceof RateLimitError) console.log(`slow down, retry in ${e.retryAfter}s`);
  else if (e instanceof ApiError) console.log(e.status, e.detail);
}

await client.logout();
```

### Restoring a session

```ts
const client = new AutoBuyClient({ baseUrl, tokens: JSON.parse(localStorage.getItem("autobuy")!) });
```

### Resource groups

Every API module is exposed as a property with one method per endpoint
(names derive from the FastAPI handler names):

| Property | Endpoints |
|---|---|
| `client.auth` | register, login, verify, refresh, logout, changePassword |
| `client.user` | getProfile, updateProfile, getUserById |
| `client.exchange` | listKeys, addOrUpdateKey, verifyKey, deleteKey |
| `client.transactions` | listTransactions, transactionStats, transactionsByCrypto, transactionsByStatus, getTransaction |
| `client.dashboard` | dashboard, quickStats, analyticsTimeline |
| `client.analytics` / `client.reports` | performance, comparison, recurring, volatility / taxSummary, exportPdf |
| `client.integrations` / `client.notifications` / `client.kyc` / `client.admin` | see `src/api.ts` |

### Low-level access

`client.transport.request<T>(method, path, { body, query, auth })` for anything
not covered, with the same auth/refresh/error behaviour.

## Development

```bash
npm install
npm run build     # -> dist/
npm test          # node:test against a mocked fetch
```

Regenerate `src/types.ts` and `src/api.ts` after an API change:

```bash
python clients/generate.py --url http://localhost:8000/openapi.json
```
