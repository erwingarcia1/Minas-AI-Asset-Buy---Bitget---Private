/**
 * Hand-written HTTP transport for the AutoBuy TypeScript client.
 *
 * Responsibilities: base URL + JSON encoding, bearer auth, transparent
 * single-flight refresh on 401, typed errors (ApiError / RateLimitError /
 * ValidationError). Works on any runtime with a WHATWG `fetch` (Node >= 18,
 * browsers, Deno, Bun); a custom `fetch` can be injected for tests.
 */

/** Login/refresh body. The refresh token is an httpOnly cookie (never in the body);
 *  `csrf_token` must be echoed as X-CSRF-Token on /refresh and /logout. */
export interface TokenPair {
  access_token: string;
  token_type?: string;
  expires_in?: number;
  csrf_token?: string;
}

export const CSRF_HEADER = "X-CSRF-Token";
const CSRF_COOKIE = "autobuy_csrf";

function csrfFromDocumentCookie(): string | undefined {
  const doc = (globalThis as { document?: { cookie?: string } }).document;
  const m = doc?.cookie?.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : undefined;
}

export interface RequestOptions {
  body?: unknown;
  query?: Record<string, unknown> | undefined;
  /** Attach the bearer token and refresh on 401 (default: true). */
  auth?: boolean;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export interface HttpTransport {
  request<T>(method: string, path: string, opts?: RequestOptions): Promise<T>;
}

export interface ValidationDetail {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

/** Any non-2xx response. `detail` is the parsed JSON body (FastAPI's `{detail}`), `body` the raw text. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly method: string,
    public readonly path: string,
    public readonly detail: unknown,
    public readonly body: string,
    public readonly headers: Headers,
  ) {
    super(`${method} ${path} -> ${status}: ${typeof detail === "string" ? detail : body.slice(0, 200)}`);
    this.name = "ApiError";
  }
}

/** 429: `retryAfter` is the server's `Retry-After` in seconds (null if absent). */
export class RateLimitError extends ApiError {
  readonly retryAfter: number | null;
  constructor(...args: ConstructorParameters<typeof ApiError>) {
    super(...args);
    this.name = "RateLimitError";
    const ra = this.headers.get("retry-after");
    this.retryAfter = ra !== null && ra !== "" && !Number.isNaN(Number(ra)) ? Number(ra) : null;
  }
}

/** 422: `errors` are the individual pydantic validation failures. */
export class ValidationError extends ApiError {
  readonly errors: ValidationDetail[];
  constructor(...args: ConstructorParameters<typeof ApiError>) {
    super(...args);
    this.name = "ValidationError";
    const d = this.detail as { detail?: ValidationDetail[] } | ValidationDetail[] | undefined;
    this.errors = Array.isArray(d) ? d : Array.isArray(d?.detail) ? d.detail : [];
  }
}

/** 401 that survived a refresh attempt (or no refresh token was available). */
export class AuthenticationError extends ApiError {
  constructor(...args: ConstructorParameters<typeof ApiError>) {
    super(...args);
    this.name = "AuthenticationError";
  }
}

export interface FetchTransportOptions {
  baseUrl: string;
  /** Initial tokens (e.g. restored from storage). */
  tokens?: TokenPair | null;
  /** Called whenever tokens change (login, refresh, logout) — persist them here. */
  onTokensChanged?: (tokens: TokenPair | null) => void;
  /** Custom fetch (tests, polyfills). Defaults to `globalThis.fetch`. */
  fetch?: typeof fetch;
  /** Extra headers on every request. */
  headers?: Record<string, string>;
  /** Path of the refresh endpoint. */
  refreshPath?: string;
}

export class FetchTransport implements HttpTransport {
  readonly baseUrl: string;
  private tokens: TokenPair | null;
  private readonly fetchImpl: typeof fetch;
  private readonly extraHeaders: Record<string, string>;
  private readonly refreshPath: string;
  private readonly onTokensChanged?: (tokens: TokenPair | null) => void;
  private refreshing: Promise<TokenPair> | null = null;

  constructor(opts: FetchTransportOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.tokens = opts.tokens ?? null;
    this.fetchImpl = opts.fetch ?? globalThis.fetch?.bind(globalThis);
    if (!this.fetchImpl) throw new Error("No fetch available: pass { fetch } explicitly");
    this.extraHeaders = opts.headers ?? {};
    this.refreshPath = opts.refreshPath ?? "/api/auth/refresh";
    this.onTokensChanged = opts.onTokensChanged;
  }

  getTokens(): TokenPair | null {
    return this.tokens;
  }

  setTokens(tokens: TokenPair | null): void {
    this.tokens = tokens;
    this.onTokensChanged?.(tokens);
  }

  /** Exchange the refresh token for a new pair. Concurrent callers share one in-flight refresh. */
  refresh(): Promise<TokenPair> {
    if (!this.refreshing) {
      this.refreshing = this.doRefresh().finally(() => {
        this.refreshing = null;
      });
    }
    return this.refreshing;
  }

  /** Browsers send the httpOnly refresh cookie themselves (`credentials: "include"`);
   *  runtimes without a cookie jar (plain Node fetch) cannot refresh and must log in again. */
  private async doRefresh(): Promise<TokenPair> {
    const csrf = this.tokens?.csrf_token ?? csrfFromDocumentCookie();
    if (!csrf) throw new Error("No session cookie available: log in again");
    const pair = await this.request<TokenPair>("POST", this.refreshPath, {
      auth: false,
      headers: { [CSRF_HEADER]: csrf },
    });
    this.setTokens(pair);
    return pair;
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const auth = opts.auth ?? true;
    const first = await this.send(method, path, opts, auth);
    if (first.status === 401 && auth && (this.tokens?.csrf_token || csrfFromDocumentCookie())) {
      try {
        await this.refresh();
      } catch {
        this.setTokens(null);
        return this.fail(first, method, path);
      }
      const second = await this.send(method, path, opts, auth);
      return this.finish<T>(second, method, path);
    }
    return this.finish<T>(first, method, path);
  }

  private buildUrl(path: string, query?: Record<string, unknown>): string {
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

  private async send(method: string, path: string, opts: RequestOptions, auth: boolean): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json", ...this.extraHeaders, ...(opts.headers ?? {}) };
    if (auth && this.tokens?.access_token) headers.Authorization = `Bearer ${this.tokens.access_token}`;
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    return this.fetchImpl(this.buildUrl(path, opts.query), { method, headers, body, signal: opts.signal, credentials: "include" });
  }

  private async finish<T>(res: Response, method: string, path: string): Promise<T> {
    if (!res.ok) return this.fail(res, method, path);
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    if (!text) return undefined as T;
    const ct = res.headers.get("content-type") ?? "";
    return (ct.includes("application/json") ? JSON.parse(text) : text) as T;
  }

  private async fail(res: Response, method: string, path: string): Promise<never> {
    const text = await res.text();
    let detail: unknown = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed && typeof parsed === "object" && "detail" in parsed ? (parsed as { detail: unknown }).detail : parsed;
    } catch {
      /* not JSON */
    }
    const args = [res.status, method, path, detail, text, res.headers] as const;
    if (res.status === 429) throw new RateLimitError(...args);
    if (res.status === 422) throw new ValidationError(...args);
    if (res.status === 401) throw new AuthenticationError(...args);
    throw new ApiError(...args);
  }
}
