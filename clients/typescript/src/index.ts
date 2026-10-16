import { CSRF_HEADER } from "./http.js";
/**
 * AutoBuy Crypto API — TypeScript client.
 *
 *   const client = new AutoBuyClient({ baseUrl: "http://localhost:8000" });
 *   await client.login("me@example.com", "Str0ng!Passw0rd");
 *   const purchase = await client.autoBuy.createPurchase({ crypto_symbol: "BTC", amount_usd: 50, frequency: "weekly", wallet_address: "bc1q...", blockchain_network: "mainnet", exchange_account_id: "<api key id>", purchase_type: "autobuy" });
 */
import { buildApi, type Api } from "./api.js";
import { FetchTransport, type FetchTransportOptions, type TokenPair } from "./http.js";
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
export function isMfaRequired(result: TokenResponse | MfaRequiredResponse): result is MfaRequiredResponse {
  return typeof result === "object" && result !== null && Boolean((result as MfaRequiredResponse).mfa_required);
}

/** High-level client: resource groups from the OpenAPI spec plus token-lifecycle helpers. */
export class AutoBuyClient {
  readonly transport: FetchTransport;
  private readonly api: Api;

  constructor(opts: AutoBuyClientOptions) {
    this.transport = new FetchTransport(opts);
    this.api = buildApi(this.transport);
  }

  // ---- resource groups (generated from the spec) --------------------------
  get system() { return this.api.system; }
  get auth() { return this.api.auth; }
  get user() { return this.api.user; }
  get exchange() { return this.api.exchange; }
  get transactions() { return this.api.transactions; }
  get dashboard() { return this.api.dashboard; }
  get integrations() { return this.api.integrations; }
  get reports() { return this.api.reports; }
  get notifications() { return this.api.notifications; }
  get admin() { return this.api.admin; }
  get analytics() { return this.api.analytics; }
  get kyc() { return this.api.kyc; }
  get webhooks() { return this.api.webhooks; }
  /** Auto-Buy purchases delivered to validated wallets (`/api/v2/purchases`). */
  get autoBuy() { return this.api.v2; }
  get wallets() { return this.api.wallets; }

  // ---- auth helpers ---------------------------------------------------------

  /** Current token pair (null when logged out). */
  get tokens(): TokenPair | null {
    return this.transport.getTokens();
  }

  /** Install a token pair obtained elsewhere (e.g. restored from storage). */
  setTokens(tokens: TokenPair | null): void {
    this.transport.setTokens(tokens);
  }

  /** `POST /api/auth/register` — create an account. Does not log in. */
  register(email: string, password: string): Promise<UserOut> {
    return this.api.auth.register({ email, password });
  }

  /**
   * `POST /api/auth/login` — authenticate and store the resulting tokens.
   *
   * An account with two-factor authentication on answers 202 with
   * `{ mfa_required: true, mfa_token, expires_in }` instead of a token pair, and nothing is
   * stored. Check the result with {@link isMfaRequired} and finish the sign-in with
   * {@link AutoBuyClient.loginMfa} within `expires_in` seconds.
   */
  async login(email: string, password: string): Promise<TokenResponse | MfaRequiredResponse> {
    const result = await this.api.auth.login({ email, password });
    if (isMfaRequired(result)) return result;
    this.transport.setTokens(result);
    return result;
  }

  /**
   * `POST /api/auth/login/mfa` — finish a two-factor sign-in and store the resulting tokens.
   * `mfaToken` is the one returned by `login()`; `factor` is `{ code }` (authenticator) or `{ recovery_code }`.
   */
  async loginMfa(mfaToken: string, factor: MfaFactor): Promise<TokenResponse> {
    if (!factor || !("code" in factor || "recovery_code" in factor)) throw new TypeError("loginMfa needs { code } or { recovery_code }");
    const pair = await this.api.auth.loginMfa({ mfa_token: mfaToken, ...factor });
    this.transport.setTokens(pair);
    return pair;
  }

  /** Force a refresh now (normally happens automatically on 401). */
  refreshTokens(): Promise<TokenPair> {
    return this.transport.refresh();
  }

  /** `POST /api/auth/logout` — revoke the refresh cookie server-side and clear local state. */
  async logout(): Promise<void> {
    const csrf = this.tokens?.csrf_token;
    await this.transport.request("POST", "/api/auth/logout", { auth: false, headers: csrf ? { [CSRF_HEADER]: csrf } : {} }).catch(() => undefined);
    this.transport.setTokens(null);
  }

  /** `GET /api/user/profile` — the authenticated user. */
  me(): Promise<UserProfile> {
    return this.api.user.getProfile();
  }

  /** Convenience wrapper around `autoBuy.createPurchase` (`POST /api/v2/purchases`). */
  createPurchase(body: PurchaseCreateRequest): Promise<PurchaseCreatedOut> {
    return this.api.v2.createPurchase(body);
  }
}

export default AutoBuyClient;
