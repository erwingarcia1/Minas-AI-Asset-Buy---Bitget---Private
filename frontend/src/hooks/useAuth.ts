import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { authAPI, userAPI } from "@/api/endpoints";
import { useAuthStore } from "@/store/authStore";

/** Login / signup / logout flows on top of the auth store. */
export function useAuth() {
  const navigate = useNavigate();
  const { setTokens, setUser, logout: clear, refreshToken, user } = useAuthStore();

  const login = useCallback(async (email: string, password: string) => {
    const tokens = await authAPI.login(email, password);
    setTokens(tokens);
    const profile = await userAPI.profile();
    setUser(profile);
    return profile;
  }, [setTokens, setUser]);

  const signup = useCallback(async (email: string, password: string) => {
    await authAPI.register(email, password);
    return login(email, password);
  }, [login]);

  const logout = useCallback(async () => {
    if (refreshToken) await authAPI.logout(refreshToken).catch(() => undefined);
    clear();
    navigate("/login", { replace: true });
  }, [refreshToken, clear, navigate]);

  const refreshProfile = useCallback(async () => {
    const profile = await userAPI.profile();
    setUser(profile);
    return profile;
  }, [setUser]);

  return { user, login, signup, logout, refreshProfile };
}
