/**
 * AutoBuy Crypto API — JavaScript client (ESM, TypeScript types bundled).
 *
 *   const client = new AutoBuyClient({ baseUrl: "http://localhost:8000" });
 *   await client.login("me@example.com", "Str0ng!Passw0rd");
 *   const purchase = await client.autoBuy.createPurchase({ crypto_symbol: "BTC", amount_usd: 50, frequency: "weekly", wallet_address: "bc1q...", blockchain_network: "mainnet", exchange_account_id: "<api key id>", purchase_type: "autobuy" });
 */
import { buildApi } from "./api.js";
import { FetchTransport } from "./http.js";

export * from "./api.js";
export { ApiError, AuthenticationError, RateLimitError, ValidationError, FetchTransport } from "./http.js";

/**
 * True when `login()` answered with a pending two-factor challenge (HTTP 202) instead of a token pair.
 * @param {import('./types.js').TokenResponse | import('./types.js').MfaRequiredResponse} result
 * @returns {result is import('./types.js').MfaRequiredResponse}
 */
export function isMfaRequired(result) {
  return typeof result === "object" && result !== null && Boolean(/** @type {import('./types.js').MfaRequiredResponse} */ (result).mfa_required);
}

/** High-level client: resource groups from the OpenAPI spec plus token-lifecycle helpers. */
export class AutoBuyClient {
  /** @param {import('./http.js').FetchTransportOptions} opts */
  constructor(opts) {
    this.transport = new FetchTransport(opts);
    const api = buildApi(this.transport);
    this.system = api.system;
    this.auth = api.auth;
    this.user = api.user;
    this.exchange = api.exchange;
    this.transactions = api.transactions;
    this.dashboard = api.dashboard;
    this.integrations = api.integrations;
    this.reports = api.reports;
    this.notifications = api.notifications;
    this.admin = api.admin;
    this.analytics = api.analytics;
    this.kyc = api.kyc;
    this.webhooks = api.webhooks;
    /** Auto-Buy purchases delivered to validated wallets (`/api/v2/purchases`). */
    this.autoBuy = api.v2;
    this.wallets = api.wallets;
  }

  /** Current token pair (null when logged out). */
  get tokens() {
    return this.transport.getTokens();
  }

  /**
   * Install a token pair obtained elsewhere (e.g. restored from storage).
   * @param {import('./http.js').TokenPair | null} tokens
   */
  setTokens(tokens) {
    this.transport.setTokens(tokens);
  }

  /**
   * `POST /api/auth/register` — create an account. Does not log in.
   * @param {string} email @param {string} password
   * @returns {Promise<import('./types.js').UserOut>}
   */
  register(email, password) {
    return this.auth.register({ email, password });
  }

  /**
   * `POST /api/auth/login` — authenticate and store the resulting tokens.
   *
   * An account with two-factor authentication on answers 202 with
   * `{ mfa_required: true, mfa_token, expires_in }` instead of a token pair, and nothing is
   * stored. Check `result.mfa_required` (or `isMfaRequired(result)`) and finish the sign-in
   * with `loginMfa()` within `expires_in` seconds.
   * @param {string} email @param {string} password
   * @returns {Promise<import('./types.js').TokenResponse | import('./types.js').MfaRequiredResponse>}
   */
  async login(email, password) {
    const result = await this.auth.login({ email, password });
    if (isMfaRequired(result)) return result;
    this.transport.setTokens(result);
    return result;
  }

  /**
   * `POST /api/auth/login/mfa` — finish a two-factor sign-in and store the resulting tokens.
   * `mfaToken` is the one returned by `login()`; `factor` is `{ code }` (authenticator) or `{ recovery_code }`.
   * @param {string} mfaToken
   * @param {{ code: string } | { recovery_code: string }} factor
   * @returns {Promise<import('./types.js').TokenResponse>}
   */
  async loginMfa(mfaToken, factor) {
    if (!factor || !("code" in factor || "recovery_code" in factor)) throw new TypeError("loginMfa needs { code } or { recovery_code }");
    const pair = await this.auth.loginMfa({ mfa_token: mfaToken, ...factor });
    this.transport.setTokens(pair);
    return pair;
  }

  /** Force a refresh now (normally happens automatically on 401). */
  refreshTokens() {
    return this.transport.refresh();
  }

  /** `POST /api/auth/logout` — revoke the refresh token and clear local state. */
  async logout() {
    const csrf = this.tokens?.csrf_token;
    await this.transport.request("POST", "/api/auth/logout", { auth: false, headers: csrf ? { "X-CSRF-Token": csrf } : {} }).catch(() => undefined);
    this.transport.setTokens(null);
  }

  /** `GET /api/user/profile` — the authenticated user. */
  me() {
    return this.user.getProfile();
  }

  /**
   * Convenience wrapper around `autoBuy.createPurchase` (`POST /api/v2/purchases`).
   * @param {import('./types.js').PurchaseCreate} body
   * @returns {Promise<import('./types.js').PurchaseResponse>}
   */
  createPurchase(body) {
    return this.autoBuy.createPurchase(body);
  }
}

export default AutoBuyClient;
