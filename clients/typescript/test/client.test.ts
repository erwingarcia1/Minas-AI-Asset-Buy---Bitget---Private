import { test } from "node:test";
import assert from "node:assert/strict";
import { AutoBuyClient, ApiError, AuthenticationError, RateLimitError, ValidationError } from "../src/index.js";

type Handler = (req: { method: string; url: URL; headers: Record<string, string>; body: unknown }) => Response | Promise<Response>;

/** Minimal mock server: a list of route handlers plus a call log. */
function mockServer() {
  const routes: Array<{ method: string; path: string; handler: Handler }> = [];
  const calls: Array<{ method: string; path: string; headers: Record<string, string>; body: unknown }> = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = Object.fromEntries(Object.entries((init?.headers as Record<string, string>) ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname + url.search, headers, body });
    const route = routes.find((r) => r.method === method && r.path === url.pathname);
    if (!route) return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404, headers: { "content-type": "application/json" } });
    return route.handler({ method, url, headers, body });
  };
  return {
    calls,
    fetch: fetchImpl,
    on(method: string, path: string, handler: Handler) {
      routes.push({ method, path, handler });
      return this;
    },
  };
}

const json = (data: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", ...headers } });

const TOKENS = { access_token: "acc-1", refresh_token: "ref-1", token_type: "bearer", expires_in: 1800 };

test("register -> login -> profile attaches bearer token", async () => {
  const srv = mockServer()
    .on("POST", "/api/auth/register", ({ body }) => json({ id: "u1", email: (body as { email: string }).email, kyc_status: "pending", created_at: "2026-09-08T00:00:00Z" }, 201))
    .on("POST", "/api/auth/login", () => json(TOKENS))
    .on("GET", "/api/user/profile", ({ headers }) =>
      headers.authorization === "Bearer acc-1" ? json({ id: "u1", email: "a@b.co", kyc_status: "pending", created_at: "x" }) : json({ detail: "nope" }, 401));
  const changes: unknown[] = [];
  const client = new AutoBuyClient({ baseUrl: "http://api.test/", fetch: srv.fetch, onTokensChanged: (t) => changes.push(t) });

  const user = await client.register("a@b.co", "Str0ng!Passw0rd");
  assert.equal(user.email, "a@b.co");
  assert.equal(srv.calls[0].headers.authorization, undefined, "register is unauthenticated");

  const pair = await client.login("a@b.co", "Str0ng!Passw0rd");
  assert.equal(pair.access_token, "acc-1");
  assert.deepEqual(client.tokens, TOKENS);
  assert.deepEqual(changes, [TOKENS]);

  const me = await client.me();
  assert.equal(me.id, "u1");
  assert.equal(srv.calls[2].headers.authorization, "Bearer acc-1");
});

test("create purchase sends JSON body and parses typed response", async () => {
  const srv = mockServer().on("POST", "/api/purchases/", ({ body }) =>
    json({ id: "p1", ...(body as object), is_active: true, next_purchase_date: "2026-09-15T00:00:00Z", created_at: "x", exchange: "binance" }, 201));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  const p = await client.purchases.createPurchase({ crypto: "BTC", amount_usdt: 50, frequency: "weekly" });
  assert.equal(p.id, "p1");
  assert.equal(p.crypto, "BTC");
  assert.equal(srv.calls[0].headers["content-type"], "application/json");
  assert.deepEqual(srv.calls[0].body, { crypto: "BTC", amount_usdt: 50, frequency: "weekly" });
});

test("list transactions encodes query params and skips undefined", async () => {
  const srv = mockServer().on("GET", "/api/transactions/", () => json({ transactions: [], total: 0, limit: 10, offset: 20 }));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  const res = await client.transactions.listTransactions({ limit: 10, offset: 20, status_filter: undefined });
  assert.equal(res.total, 0);
  assert.equal(srv.calls[0].path, "/api/transactions/?limit=10&offset=20");
});

test("path params are URL-encoded", async () => {
  const srv = mockServer().on("GET", "/api/transactions/crypto/BTC%2FX", () => json([]));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  await client.transactions.transactionsByCrypto("BTC/X");
  assert.equal(srv.calls[0].path, "/api/transactions/crypto/BTC%2FX");
});

test("401 triggers a single refresh and retries the original request", async () => {
  let access = "acc-old";
  const srv = mockServer()
    .on("GET", "/api/dashboard/stats", ({ headers }) =>
      headers.authorization === "Bearer acc-new" ? json({ total_invested_usdt: 1, transaction_count: 0, active_purchases: 0, kyc_status: "verified" }) : json({ detail: "expired" }, 401))
    .on("POST", "/api/auth/refresh", ({ body }) => {
      assert.deepEqual(body, { refresh_token: "ref-old" });
      access = "acc-new";
      return json({ access_token: access, refresh_token: "ref-new", token_type: "bearer", expires_in: 1800 });
    });
  const changes: unknown[] = [];
  const client = new AutoBuyClient({
    baseUrl: "http://api.test", fetch: srv.fetch,
    tokens: { access_token: "acc-old", refresh_token: "ref-old" }, onTokensChanged: (t) => changes.push(t),
  });
  // two concurrent calls -> one refresh (single-flight)
  const [a, b] = await Promise.all([client.dashboard.quickStats(), client.dashboard.quickStats()]);
  assert.equal(a.total_invested_usdt, 1);
  assert.equal(b.total_invested_usdt, 1);
  const refreshes = srv.calls.filter((c) => c.path === "/api/auth/refresh");
  assert.equal(refreshes.length, 1);
  assert.equal(client.tokens?.refresh_token, "ref-new");
  assert.equal(changes.length, 1);
});

test("failed refresh clears tokens and raises AuthenticationError", async () => {
  const srv = mockServer()
    .on("GET", "/api/user/profile", () => json({ detail: "expired" }, 401))
    .on("POST", "/api/auth/refresh", () => json({ detail: "revoked" }, 401));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  await assert.rejects(client.me(), (e: unknown) => e instanceof AuthenticationError && e.status === 401);
  assert.equal(client.tokens, null);
});

test("429 raises RateLimitError with retryAfter", async () => {
  const srv = mockServer().on("POST", "/api/auth/login", () => json({ detail: "Rate limit exceeded: 5 per 1 minute" }, 429, { "retry-after": "42" }));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch });
  await assert.rejects(client.login("a@b.co", "pw"), (e: unknown) => e instanceof RateLimitError && e.retryAfter === 42 && e.status === 429);
});

test("422 raises ValidationError with field errors", async () => {
  const srv = mockServer().on("POST", "/api/purchases/", () =>
    json({ detail: [{ loc: ["body", "amount_usdt"], msg: "ensure this value is >= 10", type: "value_error" }] }, 422));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  await assert.rejects(
    client.createPurchase({ crypto: "BTC", amount_usdt: 1, frequency: "weekly" }),
    (e: unknown) => e instanceof ValidationError && e.errors[0].loc[1] === "amount_usdt",
  );
});

test("other errors raise ApiError with detail", async () => {
  const srv = mockServer().on("GET", "/api/purchases/nope", () => json({ detail: "Purchase not found" }, 404));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  await assert.rejects(client.purchases.getPurchase("nope"), (e: unknown) => e instanceof ApiError && e.detail === "Purchase not found" && !(e instanceof RateLimitError));
});

test("logout revokes the refresh token and clears state", async () => {
  const srv = mockServer().on("POST", "/api/auth/logout", () => json({ detail: "ok" }));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  await client.logout();
  assert.deepEqual(srv.calls[0].body, { refresh_token: "ref-1" });
  assert.equal(client.tokens, null);
});

test("health check is unauthenticated and 204/empty bodies resolve to undefined", async () => {
  const srv = mockServer()
    .on("GET", "/health", () => json({ status: "healthy" }))
    .on("DELETE", "/api/purchases/p1", () => new Response(null, { status: 204 }));
  const client = new AutoBuyClient({ baseUrl: "http://api.test", fetch: srv.fetch, tokens: TOKENS });
  const h = (await client.system.healthCheck()) as { status: string };
  assert.equal(h.status, "healthy");
  assert.equal(srv.calls[0].headers.authorization, undefined);
  assert.equal(await client.purchases.deletePurchase("p1"), undefined);
});
