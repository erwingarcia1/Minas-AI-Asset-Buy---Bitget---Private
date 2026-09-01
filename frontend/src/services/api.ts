// Typed API client. Handles the JWT lifecycle: attaches the access token,
// transparently refreshes once on 401, and redirects to /login when the
// session is truly gone.
import axios, { AxiosError } from "axios";
import type {
  ApiKey, Dashboard, PurchaseCreate, QuickStats, RecurringPurchase,
  Timeline, TokenPair, Transaction, TransactionList, TransactionStats, User,
} from "../types";
import { clearTokens, getAccessToken, getRefreshToken, storeTokens } from "./auth";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? "http://localhost:8000",
  headers: { "Content-Type": "application/json" },
});

api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

let refreshing: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const refresh_token = getRefreshToken();
  if (!refresh_token) throw new Error("no refresh token");
  const { data } = await axios.post<TokenPair>(
    `${api.defaults.baseURL}/api/auth/refresh`, { refresh_token });
  storeTokens(data);
  return data.access_token;
}

api.interceptors.response.use(undefined, async (error: AxiosError) => {
  const original = error.config;
  if (error.response?.status === 401 && original && !original.headers["X-Retried"]) {
    try {
      refreshing = refreshing ?? refreshAccessToken();
      const token = await refreshing;
      refreshing = null;
      original.headers.Authorization = `Bearer ${token}`;
      original.headers["X-Retried"] = "1";
      return api.request(original);
    } catch {
      refreshing = null;
      clearTokens();
      if (window.location.pathname !== "/login") window.location.assign("/login");
    }
  }
  throw error;
});

export const auth = {
  register: (email: string, password: string) =>
    api.post<User>("/api/auth/register", { email, password }).then((r) => r.data),
  login: async (email: string, password: string) => {
    const { data } = await api.post<TokenPair>("/api/auth/login", { email, password });
    storeTokens(data);
    return data;
  },
  logout: async () => {
    const refresh_token = getRefreshToken();
    if (refresh_token) await api.post("/api/auth/logout", { refresh_token }).catch(() => undefined);
    clearTokens();
  },
  changePassword: (current_password: string, new_password: string) =>
    api.post("/api/auth/change-password", { current_password, new_password }),
};

export const user = {
  getProfile: () => api.get<User>("/api/user/profile").then((r) => r.data),
  updateProfile: (email: string) =>
    api.put<User>("/api/user/profile", { email }).then((r) => r.data),
};

export const kyc = {
  initiate: () =>
    api.post<{ access_token: string; applicant_id: string }>("/api/kyc/initiate")
      .then((r) => r.data),
  status: () => api.get<{ kyc_status: string }>("/api/kyc/status").then((r) => r.data),
};

export const exchange = {
  addKey: (api_key: string, api_secret: string) =>
    api.post<ApiKey>("/api/exchange/keys",
      { exchange: "binance", api_key, api_secret }).then((r) => r.data),
  listKeys: () => api.get<ApiKey[]>("/api/exchange/keys").then((r) => r.data),
  deleteKey: (id: string) => api.delete(`/api/exchange/keys/${id}`),
  verifyKey: (id: string) =>
    api.post<{ verified: boolean; result: string; detail: string;
               withdrawal_permission_warning: boolean }>(
      `/api/exchange/keys/${id}/verify`).then((r) => r.data),
};

export const purchases = {
  create: (payload: PurchaseCreate) =>
    api.post<RecurringPurchase>("/api/purchases/", payload).then((r) => r.data),
  list: () => api.get<RecurringPurchase[]>("/api/purchases/").then((r) => r.data),
  get: (id: string) => api.get<RecurringPurchase>(`/api/purchases/${id}`).then((r) => r.data),
  update: (id: string, payload: Partial<PurchaseCreate> & { enabled?: boolean }) =>
    api.put<RecurringPurchase>(`/api/purchases/${id}`, payload).then((r) => r.data),
  remove: (id: string) => api.delete(`/api/purchases/${id}`),
  toggle: (id: string) =>
    api.patch<RecurringPurchase>(`/api/purchases/${id}/toggle`).then((r) => r.data),
};

export const transactions = {
  list: (limit = 50, offset = 0, status?: string) =>
    api.get<TransactionList>("/api/transactions/",
      { params: { limit, offset, status_filter: status } }).then((r) => r.data),
  get: (id: string) => api.get<Transaction>(`/api/transactions/${id}`).then((r) => r.data),
  getStats: () =>
    api.get<TransactionStats>("/api/transactions/stats/summary").then((r) => r.data),
};

export const dashboard = {
  get: () => api.get<Dashboard>("/api/dashboard/").then((r) => r.data),
  getStats: () => api.get<QuickStats>("/api/dashboard/stats").then((r) => r.data),
  getTimeline: (days = 30) =>
    api.get<Timeline>("/api/dashboard/analytics/timeline", { params: { days } })
      .then((r) => r.data),
};
