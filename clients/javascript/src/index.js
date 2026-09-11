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
   * @param {string} email @param {string} password
   * @returns {Promise<import('./types.js').TokenResponse>}
   */
  async login(email, password) {
    const pair = await this.auth.login({ email, password });
    this.transport.setTokens(pair);
    return pair;
  }

  /** Force a refresh now (normally happens automatically on 401). */
  refreshTokens() {
    return this.transport.refresh();
  }

  /** `POST /api/auth/logout` — revoke the refresh token and clear local state. */
  async logout() {
    const refresh_token = this.tokens?.refresh_token;
    if (refresh_token) {
      await this.auth.logout({ refresh_token }).catch(() => undefined);
    }
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
