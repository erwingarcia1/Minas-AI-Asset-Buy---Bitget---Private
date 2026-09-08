export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type?: string;
  expires_in?: number;
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
  request<T = any>(method: string, path: string, opts?: RequestOptions): Promise<T>;
}

export interface ValidationDetail {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

export declare class ApiError extends Error {
  readonly status: number;
  readonly method: string;
  readonly path: string;
  readonly detail: unknown;
  readonly body: string;
  readonly headers: Headers;
  constructor(status: number, method: string, path: string, detail: unknown, body: string, headers: Headers);
}
export declare class RateLimitError extends ApiError {
  readonly retryAfter: number | null;
}
export declare class ValidationError extends ApiError {
  readonly errors: ValidationDetail[];
}
export declare class AuthenticationError extends ApiError {}

export interface FetchTransportOptions {
  baseUrl: string;
  tokens?: TokenPair | null;
  onTokensChanged?: (tokens: TokenPair | null) => void;
  fetch?: typeof fetch;
  headers?: Record<string, string>;
  refreshPath?: string;
}

export declare class FetchTransport implements HttpTransport {
  readonly baseUrl: string;
  constructor(opts: FetchTransportOptions);
  getTokens(): TokenPair | null;
  setTokens(tokens: TokenPair | null): void;
  refresh(): Promise<TokenPair>;
  request<T = any>(method: string, path: string, opts?: RequestOptions): Promise<T>;
}
