# AutoBuy Crypto — web frontend

React 18 + TypeScript + Tailwind CSS single-page app for the AutoBuy platform
API in this repository (sign up, Sumsub KYC, Auto-Buy / DCA purchases,
transaction history, settings).

## Run

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api to http://localhost:8000
npm run build      # type-check + production bundle in dist/
npm run preview    # serve dist/ locally
npm test           # vitest (jsdom)
```

The API must be running on `http://localhost:8000` (`uvicorn api.main:app --reload`
or the compose `api` service). Its `ALLOWED_ORIGINS` default already includes
`http://localhost:5173`, and `APP_BASE_URL` should be that origin so links in
emails open this app.

## API wiring

`VITE_API_BASE_URL` is `/api` in both `.env.development` and `.env.production`
(these two files are tracked; they hold no secrets). Every request therefore
goes to the same origin the page was served from:

| Where | `/api` is served by |
|---|---|
| `npm run dev` | the Vite dev server proxy → `http://localhost:8000` (see `vite.config.ts`) |
| production | nginx: the web vhost in `deployment/nginx.conf` serves `dist/` and forwards `/api/` and `/health` to the API |

To point a build at a different API host, set `VITE_API_BASE_URL` at build time
(for example `VITE_API_BASE_URL=https://api.example.com/api npm run build`) and
add the web origin to the API's `ALLOWED_ORIGINS`.

`src/api/client.ts` normalises the base so it always ends in `/api`; endpoint
paths are written relative to that (`/auth/login`, `/kyc/status`, …).

## Structure

```
src/
  api/          axios client (JWT header, single-flight refresh on 401, auto-logout) + endpoint groups
  store/        zustand auth store, persisted to localStorage ("autobuy-auth")
  hooks/        useAuth (login/signup/logout), useAsync (loader state)
  components/   Common (ProtectedRoute, Loading, ErrorBoundary, Alert, StatusBadge)
                Layout (MainContainer, Navbar, Sidebar) · Auth · KYC (Sumsub WebSDK) · Purchases
                Dashboard (Analytics, InvestmentTimeline) · Settings (Reports, Integrations)
                Admin (UserManagement, TransactionLog, AlertMonitor, ConfirmEmailDialog)
  pages/        Login, Signup, ForgotPassword, ResetPassword, Dashboard, KYC, Purchases,
                Transactions, Settings, Admin, NotFound
  types/        response/request shapes mirrored from the API's OpenAPI spec
  utils/        formatting, validation, wallet-address and exchange-network helpers
  __tests__/    vitest + Testing Library
```

Routes: `/login`, `/signup`, `/forgot-password`, `/reset-password` (public) ·
`/dashboard`, `/kyc`, `/purchases`, `/transactions`, `/settings` (protected) ·
`/admin` (protected; the link and page render only for `role = admin`, and every
`/api/admin/*` route enforces that role on the server regardless).

Analytics, projections and reports carry an "estimates only, not financial
advice; past performance doesn't guarantee future results" note.

## Content-Security-Policy

The only third-party origins the app needs are Sumsub's (`api.sumsub.com`,
`in.sumsub.com`, `static.sumsub.com`). The web vhost in
`deployment/nginx.conf` sets a CSP allowing exactly those.
