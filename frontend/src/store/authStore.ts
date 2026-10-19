import { create } from "zustand";
import type { TokenResponse, UserProfile } from "@/types";

/**
 * Session state lives in memory only. The access token is never written to
 * localStorage; the refresh token is an httpOnly cookie the browser sends to
 * /api/auth on its own. `status` is "unknown" until the silent refresh on page
 * load has answered, so route guards can wait instead of bouncing to /login.
 */
export type SessionStatus = "unknown" | "authenticated" | "anonymous";

interface AuthState {
  accessToken: string | null;
  csrfToken: string | null;
  user: UserProfile | null;
  status: SessionStatus;
  /** One-shot message shown on the login page after a forced logout. */
  notice: string | null;
  isAuthenticated: () => boolean;
  setTokens: (t: TokenResponse) => void;
  setUser: (u: UserProfile | null) => void;
  setAnonymous: () => void;
  logout: (notice?: string) => void;
  clearNotice: () => void;
}

const LEGACY_STORAGE_KEY = "autobuy-auth";

/** Older builds persisted tokens in localStorage: remove anything left behind. */
export function purgeLegacyTokenStorage(): void {
  try { localStorage.removeItem(LEGACY_STORAGE_KEY); } catch { /* storage unavailable */ }
}

export const useAuthStore = create<AuthState>()((set, get) => ({
  accessToken: null,
  csrfToken: null,
  user: null,
  status: "unknown",
  notice: null,
  isAuthenticated: () => Boolean(get().accessToken),
  setTokens: (t) => set({ accessToken: t.access_token, csrfToken: t.csrf_token, status: "authenticated" }),
  setUser: (user) => set({ user }),
  setAnonymous: () => set({ accessToken: null, csrfToken: null, user: null, status: "anonymous" }),
  logout: (notice) => set({ accessToken: null, csrfToken: null, user: null, status: "anonymous", notice: notice ?? null }),
  clearNotice: () => set({ notice: null }),
}));
