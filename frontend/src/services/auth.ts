// Token storage + session helpers. localStorage keeps this simple for now;
// an httpOnly-cookie flow is a candidate hardening for a later phase.
import type { TokenPair } from "../types";

const ACCESS = "autobuy.access_token";
const REFRESH = "autobuy.refresh_token";

export const getAccessToken = () => localStorage.getItem(ACCESS);
export const getRefreshToken = () => localStorage.getItem(REFRESH);

export function storeTokens(pair: TokenPair): void {
  localStorage.setItem(ACCESS, pair.access_token);
  localStorage.setItem(REFRESH, pair.refresh_token);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS);
  localStorage.removeItem(REFRESH);
}

export const isLoggedIn = () => Boolean(getAccessToken());
