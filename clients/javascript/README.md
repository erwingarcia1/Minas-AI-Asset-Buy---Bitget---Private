# @autobuy/js-client

Plain-JavaScript (ESM) client for the **AutoBuy Crypto API** with bundled
TypeScript declarations, so editors get full autocomplete without a build
step. Types and resource methods are generated from the API's OpenAPI spec
(`clients/openapi.json`).

- Zero dependencies — uses WHATWG `fetch` (Node ≥ 18, browsers)
- `async/await` everywhere
- Automatic single-flight JWT refresh on 401, `onTokensChanged` hook for persistence
- Typed errors: `ApiError`, `AuthenticationError`, `RateLimitError` (`retryAfter`), `ValidationError` (`errors`)

Prefer [`@autobuy/ts-client`](../typescript) if your project already compiles TypeScript.

## Installation

```bash
npm install @autobuy/js-client
# from this repo (until published):
npm install ./clients/javascript
```

## Usage

```js
import { AutoBuyClient, ApiError, RateLimitError } from "@autobuy/js-client";

const client = new AutoBuyClient({
  baseUrl: "http://localhost:8000",
  onTokensChanged: (tokens) => localStorage.setItem("autobuy", JSON.stringify(tokens)),
});

// 1. Register + login
await client.register("me@example.com", "Str0ng!Passw0rd");
await client.login("me@example.com", "Str0ng!Passw0rd");

// 2. Profile
const me = await client.me();

// 3. Schedule a recurring buy (requires verified KYC + a verified exchange key)
const purchase = await client.autoBuy.createPurchase({
  crypto: "BTC", amount_usdt: 50, frequency: "weekly",
});

// 4. History + dashboard
const page  = await client.transactions.listTransactions({ limit: 20 });
const stats = await client.dashboard.quickStats();

// 5. Errors
try {
  await client.autoBuy.getPurchase("does-not-exist");
} catch (e) {
  if (e instanceof RateLimitError) console.log(`retry in ${e.retryAfter}s`);
  else if (e instanceof ApiError) console.log(e.status, e.detail);
}

await client.logout();
```

Restore a session with `new AutoBuyClient({ baseUrl, tokens })` or `client.setTokens(tokens)`.

### Resource groups

`client.auth`, `client.user`, `client.exchange`, `client.purchases`,
`client.transactions`, `client.dashboard`, `client.analytics`, `client.reports`,
`client.integrations`, `client.notifications`, `client.kyc`,
`client.admin` — one method per endpoint, see `src/api.d.ts`.

Anything else: `client.transport.request(method, path, { body, query, auth })`.

## Development

```bash
npm test   # node:test against a mocked fetch
```

Regenerate `src/types.d.ts`, `src/api.js`, `src/api.d.ts` after an API change:

```bash
python clients/generate.py --url http://localhost:8000/openapi.json
```
