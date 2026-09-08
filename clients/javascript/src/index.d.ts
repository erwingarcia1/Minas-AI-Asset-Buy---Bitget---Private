import type * as Api from "./api.js";
import type { FetchTransport, FetchTransportOptions, TokenPair } from "./http.js";
import type { PurchaseCreate, PurchaseResponse, TokenResponse, UserOut, UserProfile } from "./types.js";

export * from "./types.js";
export * from "./api.js";
export {
  ApiError, AuthenticationError, RateLimitError, ValidationError, FetchTransport,
  type HttpTransport, type RequestOptions, type TokenPair, type FetchTransportOptions, type ValidationDetail,
} from "./http.js";

export type AutoBuyClientOptions = FetchTransportOptions;

export declare class AutoBuyClient {
  readonly transport: FetchTransport;
  readonly system: Api.SystemApi;
  readonly auth: Api.AuthApi;
  readonly user: Api.UserApi;
  readonly exchange: Api.ExchangeApi;
  readonly purchases: Api.PurchasesApi;
  readonly transactions: Api.TransactionsApi;
  readonly dashboard: Api.DashboardApi;
  readonly integrations: Api.IntegrationsApi;
  readonly reports: Api.ReportsApi;
  readonly notifications: Api.NotificationsApi;
  readonly admin: Api.AdminApi;
  readonly advanced: Api.AdvancedApi;
  readonly analytics: Api.AnalyticsApi;
  readonly kyc: Api.KycApi;
  readonly webhooks: Api.WebhooksApi;
  readonly testPurchase: Api.TestPurchaseApi;

  constructor(opts: AutoBuyClientOptions);
  get tokens(): TokenPair | null;
  setTokens(tokens: TokenPair | null): void;
  register(email: string, password: string): Promise<UserOut>;
  login(email: string, password: string): Promise<TokenResponse>;
  refreshTokens(): Promise<TokenPair>;
  logout(): Promise<void>;
  me(): Promise<UserProfile>;
  createPurchase(body: PurchaseCreate): Promise<PurchaseResponse>;
}
export default AutoBuyClient;
