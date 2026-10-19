import type * as Api from "./api.js";
import type { FetchTransport, FetchTransportOptions, TokenPair } from "./http.js";
import type { MfaRequiredResponse, PurchaseCreateRequest, PurchaseCreatedOut, TokenResponse, UserOut, UserProfile } from "./types.js";

export * from "./types.js";
export * from "./api.js";
export {
  ApiError, AuthenticationError, RateLimitError, ValidationError, FetchTransport,
  type HttpTransport, type RequestOptions, type TokenPair, type FetchTransportOptions, type ValidationDetail,
} from "./http.js";

export type AutoBuyClientOptions = FetchTransportOptions;

/** Second factor for `loginMfa`: the authenticator code or one of the recovery codes. */
export type MfaFactor = { code: string } | { recovery_code: string };

/** True when `login()` answered with a pending two-factor challenge (HTTP 202) instead of a token pair. */
export declare function isMfaRequired(result: TokenResponse | MfaRequiredResponse): result is MfaRequiredResponse;

export declare class AutoBuyClient {
  readonly transport: FetchTransport;
  readonly system: Api.SystemApi;
  readonly auth: Api.AuthApi;
  readonly user: Api.UserApi;
  readonly exchange: Api.ExchangeApi;
  readonly transactions: Api.TransactionsApi;
  readonly dashboard: Api.DashboardApi;
  readonly integrations: Api.IntegrationsApi;
  readonly reports: Api.ReportsApi;
  readonly notifications: Api.NotificationsApi;
  readonly admin: Api.AdminApi;
  readonly analytics: Api.AnalyticsApi;
  readonly kyc: Api.KycApi;
  readonly webhooks: Api.WebhooksApi;
  readonly autoBuy: Api.V2Api;
  readonly wallets: Api.WalletsApi;

  constructor(opts: AutoBuyClientOptions);
  get tokens(): TokenPair | null;
  setTokens(tokens: TokenPair | null): void;
  register(email: string, password: string): Promise<UserOut>;
  /** Answers with the `MfaRequiredResponse` challenge (nothing stored) for a two-factor account; see `loginMfa`. */
  login(email: string, password: string): Promise<TokenResponse | MfaRequiredResponse>;
  /** Finish a two-factor sign-in with the `mfa_token` from `login()` plus `{ code }` or `{ recovery_code }`; stores the tokens. */
  loginMfa(mfaToken: string, factor: MfaFactor): Promise<TokenResponse>;
  refreshTokens(): Promise<TokenPair>;
  logout(): Promise<void>;
  me(): Promise<UserProfile>;
  createPurchase(body: PurchaseCreateRequest): Promise<PurchaseCreatedOut>;
}
export default AutoBuyClient;
