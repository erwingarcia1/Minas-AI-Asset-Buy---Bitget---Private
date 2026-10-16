# AutoBuy API client SDKs

Type-safe client libraries for the AutoBuy Crypto API, generated from the
committed OpenAPI spec (`clients/openapi.json`).

| Package | Directory | Language | Install |
|---|---|---|---|
| `autobuy-client` | [`python/`](python) | Python ≥ 3.9 (sync + asyncio, `httpx`) | `pip install ./clients/python` |
| `@autobuy/js-client` | [`javascript/`](javascript) | ESM JavaScript with bundled `.d.ts` | `npm install ./clients/javascript` |
| `@autobuy/ts-client` | [`typescript/`](typescript) | TypeScript (strict, `async/await`) | `npm install ./clients/typescript` |

Every client provides:

- **Generated types** for all 71 request/response schemas and **one method per endpoint** (71 operations across 16 resource groups: `auth`, `user`, `exchange`, `v2` (Auto-Buy purchases), `wallets`, `transactions`, `dashboard`, `analytics`, `reports`, `integrations`, `notifications`, `kyc`, `admin`, ...).
- **Auth helpers**: `register`, `login` (stores tokens), `logout` (revokes), `refreshTokens`, plus transparent single-flight refresh on 401 and an `onTokensChanged` hook for persistence.
- **Typed errors**: `ApiError`, `AuthenticationError`, `RateLimitError` (with `retryAfter`), `ValidationError` (with per-field `errors`).
- **Examples** (`examples/basic.*`): register → login → create purchase → list transactions → dashboard stats → refresh → logout.
- **Unit tests** against a mocked server (no network): `pytest` / `npm test`.

## Two-factor accounts

For an account with two-factor authentication on, `POST /api/auth/login`
answers `202` with `{ mfa_required: true, mfa_token, expires_in }` instead of a
token pair. The `login()` helper returns that body as-is and stores nothing;
check `mfa_required` on the result and finish the sign-in with
`login_mfa(mfa_token, code=...)` / `loginMfa(mfaToken, { code })` (or a
`recovery_code`) within `expires_in` seconds. That second call returns the
normal token pair and stores it. The result of `login()` is typed as
`TokenResponse | MfaRequiredResponse` in every SDK; the TypeScript and
JavaScript packages also export an `isMfaRequired()` type guard.

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
