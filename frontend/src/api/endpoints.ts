import { api } from "./client";
import type {
  AdminAlert, AdminStats, AdminTxRow, AdminUserRow, ApiKey, ApplicantStatusResponse, AutoBuyPurchase, AutoBuyPurchaseCreate,
  AutoBuyPurchaseCreated, AutoBuyPurchaseUpdate, AutoBuyTransaction, CoinMarket, Comparison, CreateApplicantRequest, CreateApplicantResponse, DashboardData,
  EmailSentResponse, IntegrationInfo, KeyVerifyResponse, KycInitiateResponse, KycStatusResponse, Performance, Purchase, PurchaseCreate,
  PurchaseUpdate, QuickStats, RecurringProjection, ReportPerformance, Timeline, TokenResponse, Transaction, TransactionList,
  TransactionStats, UserProfile, VolatilityEntry, Webhook, WebhookCreated,
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
  supportedExchanges: () => data<string[]>(api.get("/exchange/supported")),
  addExchangeKey: (body: { exchange: string; api_key: string; api_secret: string; passphrase?: string }) =>
    data<ApiKey>(api.post("/exchange/keys", body)),
  verifyExchangeKey: (id: string) => data<KeyVerifyResponse>(api.post(`/exchange/keys/${id}/verify`)),
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
  timeline: (days = 30) => data<Timeline>(api.get("/dashboard/analytics/timeline", { params: { days } })),
};

/** Auto-Buy purchases: buy on an exchange account, withdraw to a validated wallet. */
export const autoBuyAPI = {
  create: (body: AutoBuyPurchaseCreate) => data<AutoBuyPurchaseCreated>(api.post("/v2/purchases", body)),
  list: () => data<AutoBuyPurchase[]>(api.get("/v2/purchases")),
  get: (id: string) => data<AutoBuyPurchase & { transactions: AutoBuyTransaction[] }>(api.get(`/v2/purchases/${id}`)),
  update: (id: string, body: AutoBuyPurchaseUpdate) =>
    data<AutoBuyPurchase>(api.put(`/v2/purchases/${id}`, body)),
  remove: (id: string) => api.delete(`/v2/purchases/${id}`),
  transactions: (id: string, status_filter?: string) =>
    data<AutoBuyTransaction[]>(api.get(`/v2/purchases/${id}/transactions`, { params: { status_filter } })),
};

export const analyticsAPI = {
  performance: () => data<Performance>(api.get("/analytics/performance")),
  recurring: () => data<RecurringProjection>(api.get("/analytics/recurring")),
  volatility: () => data<{ entries: VolatilityEntry[] }>(api.get("/analytics/volatility")),
  comparison: () => data<Comparison>(api.get("/analytics/comparison")),
};

export const reportsAPI = {
  performance: () => data<ReportPerformance>(api.get("/reports/performance")),
  /** Spreadsheet-ready FIFO lot list for a calendar year. */
  taxCsv: (year: number) => data<Blob>(api.get("/reports/tax-summary", { params: { year, format: "csv" }, responseType: "blob" })),
  /** 404 when the year has no completed purchases. */
  exportPdf: (year: number) => data<Blob>(api.get("/reports/export", { params: { year }, responseType: "blob" })),
};

export const integrationsAPI = {
  available: () => data<IntegrationInfo[]>(api.get("/integrations/available")),
  webhooks: () => data<Webhook[]>(api.get("/integrations/webhooks")),
  addWebhook: (url: string) => data<WebhookCreated>(api.post("/integrations/webhooks", { url })),
  pingWebhook: (id: string) => data<Webhook>(api.post(`/integrations/webhooks/${id}/ping`)),
  removeWebhook: (id: string) => api.delete(`/integrations/webhooks/${id}`),
};

/** Admin surface. The server enforces role=admin on every route (403 otherwise); the UI role check is cosmetic. */
export const adminAPI = {
  stats: () => data<AdminStats>(api.get("/admin/stats")),
  users: (q = "", limit = 50, offset = 0) =>
    data<{ items: AdminUserRow[]; total: number }>(api.get("/admin/users", { params: { q: q || undefined, limit, offset } })),
  suspend: (id: string) => api.post(`/admin/users/${id}/suspend`),
  unsuspend: (id: string) => api.post(`/admin/users/${id}/unsuspend`),
  /** `confirm` must equal the user's email; the server rejects anything else. */
  removeUser: (id: string, confirm: string) => api.delete(`/admin/users/${id}`, { params: { confirm } }),
  transactions: (params: { status_filter?: string; user_id?: string; limit?: number; offset?: number } = {}) =>
    data<{ items: AdminTxRow[]; total: number }>(api.get("/admin/transactions", { params })),
  alerts: () => data<AdminAlert[]>(api.get("/admin/alerts")),
};

/** Bitget market data for the purchase form: pair status and withdrawal chains with fee/minimum. */
export const marketAPI = {
  coin: (coin: string, amountUsd?: number) =>
    data<CoinMarket>(api.get(`/v2/market/coins/${encodeURIComponent(coin)}`, { params: amountUsd ? { amount_usd: amountUsd } : undefined })),
};
