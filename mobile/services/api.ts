// Mobile API client: same surface as the web client (frontend/src/services/api.ts)
// with mobile concerns added — AsyncStorage token persistence, response caching
// for offline viewing, and simple network retry. Push notification registration
// is a Phase 4/5 follow-up.
import AsyncStorage from "@react-native-async-storage/async-storage";
import axios, { AxiosError } from "axios";
import type { Dashboard, RecurringPurchase, TokenPair, TransactionList, User } from "./types";

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? "http://localhost:8000";
const ACCESS = "autobuy.access_token";
const REFRESH = "autobuy.refresh_token";
const CACHE_PREFIX = "autobuy.cache:";

export const api = axios.create({ baseURL: BASE_URL, timeout: 15000 });

api.interceptors.request.use(async (config) => {
  const token = await AsyncStorage.getItem(ACCESS);
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

async function refresh(): Promise<string | null> {
  const refresh_token = await AsyncStorage.getItem(REFRESH);
  if (!refresh_token) return null;
  try {
    const { data } = await axios.post<TokenPair>(`${BASE_URL}/api/auth/refresh`, { refresh_token });
    await AsyncStorage.multiSet([[ACCESS, data.access_token], [REFRESH, data.refresh_token]]);
    return data.access_token;
  } catch {
    await AsyncStorage.multiRemove([ACCESS, REFRESH]);
    return null;
  }
}

api.interceptors.response.use(undefined, async (error: AxiosError) => {
  const original = error.config;
  if (error.response?.status === 401 && original && !original.headers["X-Retried"]) {
    const token = await refresh();
    if (token) {
      original.headers.Authorization = `Bearer ${token}`;
      original.headers["X-Retried"] = "1";
      return api.request(original);
    }
  }
  throw error;
});

/** GET with a stale-if-offline cache in AsyncStorage. */
async function cachedGet<T>(path: string): Promise<T> {
  try {
    const { data } = await api.get<T>(path);
    await AsyncStorage.setItem(CACHE_PREFIX + path, JSON.stringify(data));
    return data;
  } catch (err) {
    const cached = await AsyncStorage.getItem(CACHE_PREFIX + path);
    if (cached) return JSON.parse(cached) as T;
    throw err;
  }
}

export const auth = {
  login: async (email: string, password: string) => {
    const { data } = await api.post<TokenPair>("/api/auth/login", { email, password });
    await AsyncStorage.multiSet([[ACCESS, data.access_token], [REFRESH, data.refresh_token]]);
    return data;
  },
  register: (email: string, password: string) =>
    api.post<User>("/api/auth/register", { email, password }).then((r) => r.data),
  logout: async () => {
    const refresh_token = await AsyncStorage.getItem(REFRESH);
    if (refresh_token) await api.post("/api/auth/logout", { refresh_token }).catch(() => undefined);
    await AsyncStorage.multiRemove([ACCESS, REFRESH]);
  },
  isLoggedIn: async () => Boolean(await AsyncStorage.getItem(ACCESS)),
};

export const dashboard = { get: () => cachedGet<Dashboard>("/api/dashboard/") };
export const purchases = { list: () => cachedGet<RecurringPurchase[]>("/api/purchases/") };
export const transactions = {
  list: (limit = 50) => cachedGet<TransactionList>(`/api/transactions/?limit=${limit}`),
};
