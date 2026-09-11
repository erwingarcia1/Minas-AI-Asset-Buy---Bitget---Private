import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { TokenResponse, UserProfile } from "@/types";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: UserProfile | null;
  /** One-shot message shown on the login page after a forced logout. */
  notice: string | null;
  isAuthenticated: () => boolean;
  setTokens: (t: TokenResponse) => void;
  setUser: (u: UserProfile | null) => void;
  logout: (notice?: string) => void;
  clearNotice: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      notice: null,
      isAuthenticated: () => Boolean(get().accessToken),
      setTokens: (t) => set({ accessToken: t.access_token, refreshToken: t.refresh_token }),
      setUser: (user) => set({ user }),
      logout: (notice) => set({ accessToken: null, refreshToken: null, user: null, notice: notice ?? null }),
      clearNotice: () => set({ notice: null }),
    }),
    {
      name: "autobuy-auth",
      partialize: (s) => ({ accessToken: s.accessToken, refreshToken: s.refreshToken, user: s.user }),
    },
  ),
);
