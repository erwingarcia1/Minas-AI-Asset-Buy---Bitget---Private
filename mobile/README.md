# AutoBuy Mobile (scaffold)

Expo (managed React Native) scaffold sharing the platform API and types with
the web frontend. Screens are stubs — full implementation is Phase 4/5 work.

## Setup

```bash
npm install
npx expo start        # then: i = iOS simulator, a = Android emulator
```

Set `EXPO_PUBLIC_API_URL` for a non-localhost API. Type check:
`npm run typecheck` (requires `npm install` first — heavy Expo dependency tree,
intentionally not vendored in this repo). Release builds: `eas build`.

Already mobile-aware in `services/api.ts`: AsyncStorage token persistence,
transparent refresh, and stale-if-offline caching of dashboard/purchase/
transaction reads. Push notifications are a Phase 4/5 follow-up.
