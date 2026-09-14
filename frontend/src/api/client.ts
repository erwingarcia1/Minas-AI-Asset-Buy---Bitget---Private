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
  withCredentials: true,   // the refresh token is an httpOnly cookie on /api/auth
});

export const CSRF_HEADER = "X-CSRF-Token";
const CSRF_COOKIE = "autobuy_csrf";

/** The CSRF token: from the last login/refresh response, else the readable cookie. */
export function csrfToken(): string | null {
  const inMemory = useAuthStore.getState().csrfToken;
  if (inMemory) return inMemory;
  const m = typeof document !== "undefined" ? document.cookie.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`)) : null;
  return m ? decodeURIComponent(m[1]) : null;
}

// ---- request: attach the bearer token ---------------------------------------
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// ---- response: refresh once on 401, otherwise log out ------------------------
type RetriableConfig = InternalAxiosRequestConfig & { _retried?: boolean };
let refreshing: Promise<string> | null = null;

/** Silent refresh from the httpOnly cookie. Plain axios: must not go through the interceptor. */
export async function refreshAccessToken(): Promise<string> {
  const csrf = csrfToken();
  const { data } = await axios.post<TokenResponse>(`${API_BASE_URL}/auth/refresh`, undefined, {
    withCredentials: true,
    headers: csrf ? { [CSRF_HEADER]: csrf } : {},
  });
  useAuthStore.getState().setTokens(data);
  return data.access_token;
}

export const SESSION_EXPIRED_NOTICE = "Your session expired, please sign in again.";

/** Forget the readable CSRF cookie (the httpOnly refresh cookie is cleared by the API). */
function forgetCsrfCookie(): void {
  if (typeof document === "undefined") return;
  document.cookie = `${CSRF_COOKIE}=; max-age=0; path=/`;
  document.cookie = `${CSRF_COOKIE}=; max-age=0; path=/api/auth`;
}

/**
 * On page load: try the cookie once. A visitor with no session becomes
 * anonymous silently; a visitor who HAD a session (the CSRF cookie is there)
 * but cannot refresh it is told the session expired, never left guessing.
 */
export async function bootstrapSession(): Promise<boolean> {
  const hadSession = csrfToken() !== null;
  try {
    refreshing = refreshing ?? refreshAccessToken();
    await refreshing;
    return true;
  } catch {
    if (hadSession) {
      forgetCsrfCookie();
      useAuthStore.getState().logout(SESSION_EXPIRED_NOTICE);
    } else {
      useAuthStore.getState().setAnonymous();
    }
    return false;
  } finally {
    refreshing = null;
  }
}

api.interceptors.response.use(undefined, async (error: AxiosError<ApiErrorBody>) => {
  const original = error.config as RetriableConfig | undefined;
  const status = error.response?.status;
  const isAuthRoute = original?.url?.startsWith("/auth/");

  if (status === 401 && original && !original._retried && !isAuthRoute) {
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
    forgetCsrfCookie();
    useAuthStore.getState().logout(SESSION_EXPIRED_NOTICE);
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
