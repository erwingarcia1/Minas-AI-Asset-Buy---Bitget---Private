import axios, { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from "axios";
import { useAuthStore } from "@/store/authStore";
import type { ApiErrorBody, TokenResponse } from "@/types";

/**
 * VITE_API_BASE_URL is the API *host* in development (http://localhost:8000)
 * and the proxied path (/api) in production. Every platform route lives under
 * /api, so normalise to a base that already ends in /api and let endpoints use
 * paths like "/auth/login".
 */
const raw = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");
export const API_BASE_URL = raw.endsWith("/api") ? raw : `${raw}/api`;

export const api = axios.create({
  baseURL: API_BASE_URL,
  headers: { "Content-Type": "application/json" },
  timeout: 20_000,
});

// ---- request: attach the bearer token ---------------------------------------
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// ---- response: refresh once on 401, otherwise log out ------------------------
type RetriableConfig = InternalAxiosRequestConfig & { _retried?: boolean };
let refreshing: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const { refreshToken, setTokens } = useAuthStore.getState();
  if (!refreshToken) throw new Error("no refresh token");
  // Plain axios: must not go through the interceptor that brought us here.
  const { data } = await axios.post<TokenResponse>(`${API_BASE_URL}/auth/refresh`, {
    refresh_token: refreshToken,
  });
  setTokens(data);
  return data.access_token;
}

api.interceptors.response.use(undefined, async (error: AxiosError<ApiErrorBody>) => {
  const original = error.config as RetriableConfig | undefined;
  const status = error.response?.status;
  const isAuthRoute = original?.url?.startsWith("/auth/");

  if (status === 401 && original && !original._retried && !isAuthRoute) {
    const { refreshToken } = useAuthStore.getState();
    if (refreshToken) {
      try {
        refreshing = refreshing ?? refreshAccessToken();
        const token = await refreshing;
        refreshing = null;
        original._retried = true;
        original.headers.Authorization = `Bearer ${token}`;
        return api.request(original);
      } catch {
        refreshing = null;
      }
    }
    useAuthStore.getState().logout("Your session has expired. Please sign in again.");
  }
  throw error;
});

// ---- helpers -------------------------------------------------------------------
/** Human-readable message from any axios/FastAPI error. */
export function errorMessage(err: unknown, fallback = "Something went wrong"): string {
  if (axios.isAxiosError<ApiErrorBody>(err)) {
    const body = err.response?.data;
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail.map((d) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ");
    }
    if (body?.detail && typeof body.detail === "object") {
      const d = body.detail as { error?: string; details?: { message?: string; supported_networks?: string[] } };
      if (d.details?.message) return d.details.message;
      if (d.error === "network_not_supported_by_exchange")
        return `That exchange cannot withdraw to this network (supports: ${d.details?.supported_networks?.join(", ") ?? "none"})`;
      if (d.error) return d.error.replace(/_/g, " ");
    }
    if (typeof body?.error === "string") return body.error;
    if (err.response?.status === 429) return "Too many requests. Please wait a moment.";
    if (!err.response) return "Cannot reach the API. Is it running?";
    return `${err.response.status} ${err.response.statusText}`;
  }
  return err instanceof Error ? err.message : fallback;
}

export type { AxiosRequestConfig };
