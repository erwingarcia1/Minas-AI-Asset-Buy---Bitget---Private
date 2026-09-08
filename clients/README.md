# AutoBuy API client SDKs

Type-safe client libraries for the AutoBuy Crypto API, generated from the
committed OpenAPI spec (`clients/openapi.json`).

| Package | Directory | Language | Install |
|---|---|---|---|
| `autobuy-client` | [`python/`](python) | Python ≥ 3.9 (sync + asyncio, `httpx`) | `pip install ./clients/python` |
| `@autobuy/js-client` | [`javascript/`](javascript) | ESM JavaScript with bundled `.d.ts` | `npm install ./clients/javascript` |
| `@autobuy/ts-client` | [`typescript/`](typescript) | TypeScript (strict, `async/await`) | `npm install ./clients/typescript` |

Every client provides:

- **Generated types** for all 61 request/response schemas and **one method per endpoint** (65 operations across 16 resource groups: `auth`, `user`, `exchange`, `purchases`, `transactions`, `dashboard`, `analytics`, `reports`, `integrations`, `notifications`, `advanced`, `kyc`, `admin`, ...).
- **Auth helpers**: `register`, `login` (stores tokens), `logout` (revokes), `refreshTokens`, plus transparent single-flight refresh on 401 and an `onTokensChanged` hook for persistence.
- **Typed errors**: `ApiError`, `AuthenticationError`, `RateLimitError` (with `retryAfter`), `ValidationError` (with per-field `errors`).
- **Examples** (`examples/basic.*`): register → login → create purchase → list transactions → dashboard stats → refresh → logout.
- **Unit tests** against a mocked server (no network): `pytest` / `npm test`.

## Regenerating after an API change

```bash
# with the API running locally
python clients/generate.py --url http://localhost:8000/openapi.json
# or from the committed spec
python clients/generate.py
```

`generate.py` rewrites only the generated files (`types.*`, `api.*`); the
HTTP/auth layers (`http.ts`, `http.js`, `_http.py`) are hand-written and stable.
Method names derive from the FastAPI handler names (`list_transactions` →
`listTransactions` in JS/TS), so renaming a handler renames the client method.

## Verifying

```bash
(cd clients/python     && python -m pytest)
(cd clients/javascript && npm test)
(cd clients/typescript && npm install && npm test && npm run build)
```

## Publishing

- Python: `cd clients/python && poetry build && poetry publish` (or `python -m build`).
- npm: `cd clients/typescript && npm run build && npm publish --access public`; same for `javascript/` (no build step).

Bump the version in `pyproject.toml` / `setup.py` / `package.json` together
with the API's version.
