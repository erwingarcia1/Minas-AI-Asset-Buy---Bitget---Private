import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { authAPI, userAPI } from "@/api/endpoints";
import { refreshAccessToken } from "@/api/client";
import { useAuthStore } from "@/store/authStore";
import type { MfaFactor, UserProfile } from "@/types";

export const SIGN_OUT_FAILED = "Sign-out failed, try again.";

/** A password login either signs the user in or asks for the second factor. */
export type LoginResult = { profile: UserProfile; mfaToken?: undefined } | { mfaToken: string; profile?: undefined };

/** Login / signup / logout flows on top of the auth store. */
export function useAuth() {
  const navigate = useNavigate();
  const { setTokens, setUser, logout: clear, user } = useAuthStore();

  const login = useCallback(async (email: string, password: string): Promise<LoginResult> => {
    const outcome = await authAPI.login(email, password);
    if (outcome.kind === "mfa") return { mfaToken: outcome.mfaToken };   // no session yet: the code step follows
    setTokens(outcome.tokens);
    const profile = await userAPI.profile();
    setUser(profile);
    return { profile };
  }, [setTokens, setUser]);

  /** Finish a two-factor sign-in with an authenticator code or a recovery code. */
  const completeMfa = useCallback(async (mfaToken: string, factor: MfaFactor) => {
    const tokens = await authAPI.loginMfa(mfaToken, factor);
    setTokens(tokens);
    const profile = await userAPI.profile();
    setUser(profile);
    return profile;
  }, [setTokens, setUser]);

  const signup = useCallback(async (email: string, password: string, inviteCode?: string) => {
    await authAPI.register(email, password, inviteCode);
    const result = await login(email, password);
    if (!result.profile) throw new Error("Unexpected sign-in step after registration");   // a new account has no second factor
    return result.profile;
  }, [login]);

  /** Sign out everywhere: the server refuses every token issued before now; this tab goes to the login page. */
  const signOutEverywhere = useCallback(async () => {
    await authAPI.logoutAll();
    clear("You were signed out everywhere. Sign in again to continue.");
    navigate("/login", { replace: true });
  }, [clear, navigate]);

  /** Server-side revocation first; the local state is cleared only when the server confirmed.
   *  A first failure is retried once with a freshly refreshed CSRF value (another tab may have
   *  rotated the cookies); a second failure is reported, and the user stays signed in. */
  const logout = useCallback(async () => {
    try {
      await authAPI.logout();
    } catch {
      try {
        await refreshAccessToken().catch(() => undefined);   // a dead session still lets logout answer 204
        await authAPI.logout();
      } catch {
        throw new Error(SIGN_OUT_FAILED);
      }
    }
    clear();
    navigate("/login", { replace: true });
  }, [clear, navigate]);

  const refreshProfile = useCallback(async () => {
    const profile = await userAPI.profile();
    setUser(profile);
    return profile;
  }, [setUser]);

  return { user, login, completeMfa, signup, logout, signOutEverywhere, refreshProfile };
}
