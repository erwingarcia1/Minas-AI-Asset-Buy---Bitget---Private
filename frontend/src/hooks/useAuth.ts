import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { authAPI, userAPI } from "@/api/endpoints";
import { refreshAccessToken } from "@/api/client";
import { useAuthStore } from "@/store/authStore";

export const SIGN_OUT_FAILED = "Sign-out failed, try again.";

/** Login / signup / logout flows on top of the auth store. */
export function useAuth() {
  const navigate = useNavigate();
  const { setTokens, setUser, logout: clear, user } = useAuthStore();

  const login = useCallback(async (email: string, password: string) => {
    const tokens = await authAPI.login(email, password);
    setTokens(tokens);
    const profile = await userAPI.profile();
    setUser(profile);
    return profile;
  }, [setTokens, setUser]);

  const signup = useCallback(async (email: string, password: string, inviteCode?: string) => {
    await authAPI.register(email, password, inviteCode);
    return login(email, password);
  }, [login]);

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

  return { user, login, signup, logout, refreshProfile };
}
