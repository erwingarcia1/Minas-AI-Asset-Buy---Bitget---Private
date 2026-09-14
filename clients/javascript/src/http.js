/**
 * Hand-written HTTP transport for the AutoBuy JavaScript client.
 *
 * Bearer auth, single-flight refresh on 401, typed errors. Uses the global
 * WHATWG `fetch` (Node >= 18, browsers); a custom one can be injected for tests.
 */

/** Any non-2xx response. `detail` is the parsed JSON `detail`, `body` the raw text. */
export const CSRF_HEADER = "X-CSRF-Token";
const CSRF_COOKIE = "autobuy_csrf";
function csrfFromDocumentCookie() {
  const m = globalThis.document?.cookie?.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : undefined;
}

export class ApiError extends Error {
  /**
   * @param {number} status
   * @param {string} method
   * @param {string} path
   * @param {unknown} detail
   * @param {string} body
   * @param {Headers} headers
   */
  constructor(status, method, path, detail, body, headers) {
    super(`${method} ${path} -> ${status}: ${typeof detail === "string" ? detail : body.slice(0, 200)}`);
    this.name = "ApiError";
    this.status = status;
    this.method = method;
    this.path = path;
    this.detail = detail;
    this.body = body;
    this.headers = headers;
  }
}

/** 429: `retryAfter` is the server's `Retry-After` in seconds (null if absent). */
export class RateLimitError extends ApiError {
  /** @param {ConstructorParameters<typeof ApiError>} args */
  constructor(...args) {
    super(...args);
    this.name = "RateLimitError";
    const ra = this.headers.get("retry-after");
    this.retryAfter = ra !== null && ra !== "" && !Number.isNaN(Number(ra)) ? Number(ra) : null;
  }
}

/** 422: `errors` are the individual pydantic validation failures. */
export class ValidationError extends ApiError {
  /** @param {ConstructorParameters<typeof ApiError>} args */
  constructor(...args) {
    super(...args);
    this.name = "ValidationError";
    const d = this.detail;
    this.errors = Array.isArray(d) ? d : Array.isArray(d?.detail) ? d.detail : [];
  }
}

/** 401 that survived a refresh attempt (or no refresh token was available). */
export class AuthenticationError extends ApiError {
  /** @param {ConstructorParameters<typeof ApiError>} args */
  constructor(...args) {
    super(...args);
    this.name = "AuthenticationError";
  }
}

export class FetchTransport {
  /**
   * @param {object} opts
   * @param {string} opts.baseUrl
   * @param {import('./http.js').TokenPair | null} [opts.tokens]
   * @param {(tokens: import('./http.js').TokenPair | null) => void} [opts.onTokensChanged]
   * @param {typeof fetch} [opts.fetch]
   * @param {Record<string, string>} [opts.headers]
   * @param {string} [opts.refreshPath]
   */
  constructor(opts) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    /** @type {import('./http.js').TokenPair | null} */
    this._tokens = opts.tokens ?? null;
    this._fetch = opts.fetch ?? globalThis.fetch?.bind(globalThis);
    if (!this._fetch) throw new Error("No fetch available: pass { fetch } explicitly");
    this._extraHeaders = opts.headers ?? {};
    this._refreshPath = opts.refreshPath ?? "/api/auth/refresh";
    this._onTokensChanged = opts.onTokensChanged;
    /** @type {Promise<import('./http.js').TokenPair> | null} */
    this._refreshing = null;
  }

  getTokens() {
    return this._tokens;
  }

  /** @param {import('./http.js').TokenPair | null} tokens */
  setTokens(tokens) {
    this._tokens = tokens;
    this._onTokensChanged?.(tokens);
  }

  /** Exchange the refresh token for a new pair. Concurrent callers share one in-flight refresh. */
  refresh() {
    if (!this._refreshing) {
      this._refreshing = this._doRefresh().finally(() => {
        this._refreshing = null;
      });
    }
    return this._refreshing;
  }

  /** The refresh token is an httpOnly cookie: browsers send it themselves (credentials: "include");
   *  runtimes without a cookie jar cannot refresh and must log in again. */
  async _doRefresh() {
    const csrf = this._tokens?.csrf_token ?? csrfFromDocumentCookie();
    if (!csrf) throw new Error("No session cookie available: log in again");
    const pair = await this.request("POST", this._refreshPath, { auth: false, headers: { [CSRF_HEADER]: csrf } });
    this.setTokens(pair);
    return pair;
  }

  /**
   * @param {string} method
   * @param {string} path
   * @param {import('./http.js').RequestOptions} [opts]
   * @returns {Promise<any>}
   */
  async request(method, path, opts = {}) {
    const auth = opts.auth ?? true;
    const first = await this._send(method, path, opts, auth);
    if (first.status === 401 && auth && (this._tokens?.csrf_token || csrfFromDocumentCookie())) {
      try {
        await this.refresh();
      } catch {
        this.setTokens(null);
        return this._fail(first, method, path);
      }
      const second = await this._send(method, path, opts, auth);
      return this._finish(second, method, path);
    }
    return this._finish(first, method, path);
  }

  /** @param {string} path @param {Record<string, unknown>} [query] */
  _buildUrl(path, query) {
    let url = this.baseUrl + (path.startsWith("/") ? path : "/" + path);
    if (query) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v === undefined || v === null) continue;
        qs.set(k, String(v));
      }
      const s = qs.toString();
      if (s) url += (url.includes("?") ? "&" : "?") + s;
    }
    return url;
  }

  /** @param {string} method @param {string} path @param {import('./http.js').RequestOptions} opts @param {boolean} auth */
  async _send(method, path, opts, auth) {
    /** @type {Record<string, string>} */
    const headers = { Accept: "application/json", ...this._extraHeaders, ...(opts.headers ?? {}) };
    if (auth && this._tokens?.access_token) headers.Authorization = `Bearer ${this._tokens.access_token}`;
    let body;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    return this._fetch(this._buildUrl(path, opts.query), { method, headers, body, signal: opts.signal, credentials: "include" });
  }

  /** @param {Response} res @param {string} method @param {string} path */
  async _finish(res, method, path) {
    if (!res.ok) return this._fail(res, method, path);
    if (res.status === 204) return undefined;
    const text = await res.text();
    if (!text) return undefined;
    const ct = res.headers.get("content-type") ?? "";
    return ct.includes("application/json") ? JSON.parse(text) : text;
  }

  /** @param {Response} res @param {string} method @param {string} path @returns {Promise<never>} */
  async _fail(res, method, path) {
    const text = await res.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed && typeof parsed === "object" && "detail" in parsed ? parsed.detail : parsed;
    } catch {
      /* not JSON */
    }
    const args = /** @type {const} */ ([res.status, method, path, detail, text, res.headers]);
    if (res.status === 429) throw new RateLimitError(...args);
    if (res.status === 422) throw new ValidationError(...args);
    if (res.status === 401) throw new AuthenticationError(...args);
    throw new ApiError(...args);
  }
}
