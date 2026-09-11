import { api } from "./client";
import type {
  ApiKey, ApplicantStatusResponse, AutoBuyPurchase, AutoBuyPurchaseCreate, AutoBuyPurchaseCreated, AutoBuyTransaction, CreateApplicantRequest, CreateApplicantResponse,
  DashboardData, EmailSentResponse, KycInitiateResponse, KycStatusResponse, Purchase,
  PurchaseCreate, PurchaseUpdate, QuickStats, TokenResponse, Transaction, TransactionList,
  TransactionStats, UserProfile,
} from "@/types";

const data = <T>(p: Promise<{ data: T }>) => p.then((r) => r.data);

export const authAPI = {
  register: (email: string, password: string) =>
    data<UserProfile>(api.post("/auth/register", { email, password })),
  login: (email: string, password: string) =>
    data<TokenResponse>(api.post("/auth/login", { email, password })),
  refresh: (refresh_token: string) =>
    data<TokenResponse>(api.post("/auth/refresh", { refresh_token })),
  logout: (refresh_token: string) => api.post("/auth/logout", { refresh_token }),
  changePassword: (current_password: string, new_password: string) =>
    api.post("/auth/change-password", { current_password, new_password }),
  /** Always resolves `{sent: true}`; the API never reveals whether the email has an account. */
  forgotPassword: (email: string) => data<{ sent: boolean }>(api.post("/auth/forgot-password", { email })),
  /** true when /reset-password would accept the token; does not consume it. 400 -> {valid: false}. */
  validateResetToken: (token: string) => data<{ valid: boolean }>(api.post("/auth/reset-password/validate", { token })),
  /** 400 = invalid/expired/used link, 422 = password policy. */
  resetPassword: (token: string, new_password: string) => api.post("/auth/reset-password", { token, new_password }),
};

export const userAPI = {
  profile: () => data<UserProfile>(api.get("/user/profile")),
  updateProfile: (email: string) => data<UserProfile>(api.put("/user/profile", { email })),
  sendVerification: (email: string) =>
    data<EmailSentResponse>(api.post("/email/send-verification", { email })),
  exchangeKeys: () => data<ApiKey[]>(api.get("/exchange/keys")),
  addExchangeKey: (body: { exchange: string; api_key: string; api_secret: string; passphrase?: string }) =>
    data<ApiKey>(api.post("/exchange/keys", body)),
  verifyExchangeKey: (id: string) => data<unknown>(api.post(`/exchange/keys/${id}/verify`)),
  deleteExchangeKey: (id: string) => api.delete(`/exchange/keys/${id}`),
};

export const kycAPI = {
  /** Stored status; while pending it is synced from Sumsub. `sync` forces a Sumsub lookup even when final. */
  status: (sync = false) => data<KycStatusResponse>(api.get("/kyc/status", { params: sync ? { sync: 1 } : undefined })),
  initiate: () => data<KycInitiateResponse>(api.post("/kyc/initiate")),
  createApplicant: (body: CreateApplicantRequest) =>
    data<CreateApplicantResponse>(api.post("/kyc/create-applicant", body)),
  applicantStatus: (applicantId: string) =>
    data<ApplicantStatusResponse>(api.get(`/kyc/applicant/${encodeURIComponent(applicantId)}/status`)),
};

export const purchaseAPI = {
  list: () => data<Purchase[]>(api.get("/purchases/")),
  get: (id: string) => data<Purchase>(api.get(`/purchases/${id}`)),
  create: (body: PurchaseCreate) => data<Purchase>(api.post("/purchases/", body)),
  update: (id: string, body: PurchaseUpdate) => data<Purchase>(api.put(`/purchases/${id}`, body)),
  toggle: (id: string) => data<Purchase>(api.patch(`/purchases/${id}/toggle`)),
  remove: (id: string) => api.delete(`/purchases/${id}`),
};

export const transactionAPI = {
  list: (params: { limit?: number; offset?: number; status_filter?: string } = {}) =>
    data<TransactionList>(api.get("/transactions/", { params })),
  get: (id: string) => data<Transaction>(api.get(`/transactions/${id}`)),
  stats: () => data<TransactionStats>(api.get("/transactions/stats/summary")),
  byCrypto: (crypto: string) => data<Transaction[]>(api.get(`/transactions/crypto/${crypto}`)),
};

export const dashboardAPI = {
  overview: () => data<DashboardData>(api.get("/dashboard/")),
  quickStats: () => data<QuickStats>(api.get("/dashboard/stats")),
};

/** Auto-Buy purchases: buy on an exchange account, withdraw to a validated wallet. */
export const autoBuyAPI = {
  create: (body: AutoBuyPurchaseCreate) => data<AutoBuyPurchaseCreated>(api.post("/v2/purchases", body)),
  list: () => data<AutoBuyPurchase[]>(api.get("/v2/purchases")),
  get: (id: string) => data<AutoBuyPurchase & { transactions: AutoBuyTransaction[] }>(api.get(`/v2/purchases/${id}`)),
  update: (id: string, body: { status?: "active" | "paused"; amount_usd?: number; frequency?: AutoBuyPurchaseCreate["frequency"] }) =>
    data<AutoBuyPurchase>(api.put(`/v2/purchases/${id}`, body)),
  remove: (id: string) => api.delete(`/v2/purchases/${id}`),
  transactions: (id: string, status_filter?: string) =>
    data<AutoBuyTransaction[]>(api.get(`/v2/purchases/${id}/transactions`, { params: { status_filter } })),
};
