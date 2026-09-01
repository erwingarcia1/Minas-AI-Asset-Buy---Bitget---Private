# AutoBuy Frontend

React + TypeScript + Vite + Tailwind scaffold for the AutoBuy Crypto Platform.

## Setup

```bash
npm install
npm run dev        # http://localhost:3000 (API assumed at http://localhost:8000)
```

Set `VITE_API_URL` to point at a different API. Build: `npm run build`
(runs the type check first). Tests: `npm test`. 

The typed API client (`src/services/api.ts`) covers every backend endpoint and
handles the JWT lifecycle (attach, transparent refresh on 401, redirect to
/login when the session is gone). `src/types/index.ts` mirrors the OpenAPI
schemas (v0.2.0).

Component stubs under `src/components/` are Phase 4 work — the dashboard and
purchase management screens are implemented first (Sprint 4).
