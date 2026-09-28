import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { post, instanceRequest, responseInterceptors } = vi.hoisted(() => ({
  post: vi.fn(), instanceRequest: vi.fn(), responseInterceptors: [] as Array<(err: unknown) => unknown>,
}));
vi.mock("axios", () => {
  const instance = { interceptors: { request: { use: vi.fn() }, response: { use: (_ok: unknown, onError: (err: unknown) => unknown) => responseInterceptors.push(onError) } },
    request: (...a: unknown[]) => instanceRequest(...a), post: vi.fn(), get: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: { create: () => instance, post: (...a: unknown[]) => post(...a), isAxiosError: () => false }, AxiosError: class {} };
});
import { RATE_LIMITED_NOTICE, SESSION_EXPIRED_NOTICE, bootstrapSession, csrfToken, refreshAccessToken } from "@/api/client";
import { ProtectedRoute, PublicOnlyRoute } from "@/components/Common/ProtectedRoute";
import { purgeLegacyTokenStorage, useAuthStore } from "@/store/authStore";

const TOKENS = { access_token: "acc-123", token_type: "bearer", expires_in: 1800, csrf_token: "csrf-abc" };

describe("session: tokens stay out of localStorage", () => {
  beforeEach(() => {
    localStorage.clear();
    document.cookie = "autobuy_csrf=; max-age=0; path=/";
    useAuthStore.setState({ accessToken: null, csrfToken: null, user: null, status: "unknown", notice: null });
    post.mockReset();
  });

  it("the access token never appears in localStorage after login or refresh", async () => {
    useAuthStore.getState().setTokens(TOKENS);
    expect(useAuthStore.getState().accessToken).toBe("acc-123");
    expect(localStorage.length).toBe(0);
    expect(Object.keys(localStorage)).not.toContain("autobuy-auth");
    expect(JSON.stringify(localStorage)).not.toContain("acc-123");
  });

  it("purges tokens an older build persisted", () => {
    localStorage.setItem("autobuy-auth", JSON.stringify({ state: { accessToken: "old", refreshToken: "old-refresh" } }));
    purgeLegacyTokenStorage();
    expect(localStorage.getItem("autobuy-auth")).toBeNull();
  });

  it("bootstrap refreshes from the cookie with the CSRF header and never sends a body", async () => {
    document.cookie = "autobuy_csrf=cookie-csrf; path=/";
    post.mockResolvedValue({ data: TOKENS });
    expect(csrfToken()).toBe("cookie-csrf");
    expect(await bootstrapSession()).toBe(true);
    expect(post).toHaveBeenCalledWith(expect.stringMatching(/\/auth\/refresh$/), undefined,
      expect.objectContaining({ withCredentials: true, headers: { "X-CSRF-Token": "cookie-csrf" } }));
    const s = useAuthStore.getState();
    expect(s.status).toBe("authenticated");
    expect(s.accessToken).toBe("acc-123");
    expect(s.csrfToken).toBe("csrf-abc");   // the in-memory token from the response wins from now on
    expect(csrfToken()).toBe("csrf-abc");
    expect(localStorage.length).toBe(0);
  });

  it("bootstrap without any session marks the visitor anonymous, silently", async () => {
    post.mockRejectedValue(new Error("401"));
    expect(await bootstrapSession()).toBe(false);
    expect(useAuthStore.getState().status).toBe("anonymous");
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(useAuthStore.getState().notice).toBeNull();
    await expect(refreshAccessToken()).rejects.toThrow();
  });

  it("bootstrap with a session cookie that no longer refreshes tells the user the session expired", async () => {
    document.cookie = "autobuy_csrf=stale; path=/";
    post.mockRejectedValue({ response: { status: 401 } });
    expect(await bootstrapSession()).toBe(false);
    const s = useAuthStore.getState();
    expect(s.status).toBe("anonymous");
    expect(s.notice).toBe(SESSION_EXPIRED_NOTICE);
    expect(document.cookie).not.toContain("autobuy_csrf=stale");   // no repeated notices on the next load
  });

  it("REPRODUCTION: a CSRF cookie scoped to /api/auth is invisible to the page, so refresh would have no header", () => {
    // jsdom serves the test page at http://localhost:3000/ ; cookie path rules apply to document.cookie
    document.cookie = "autobuy_csrf=scoped; path=/api/auth";
    expect(document.cookie).not.toContain("autobuy_csrf");
    expect(csrfToken()).toBeNull();
    // the fix: the API sets it on path=/ (readable on every route), the refresh cookie stays on /api/auth
    document.cookie = "autobuy_csrf=visible; path=/";
    expect(csrfToken()).toBe("visible");
  });

  it("a short-lived access token is refreshed silently on 401 and the request is retried", async () => {
    document.cookie = "autobuy_csrf=cookie-csrf; path=/";
    useAuthStore.getState().setTokens({ ...TOKENS, access_token: "expired-acc", csrf_token: "cookie-csrf" });
    post.mockResolvedValue({ data: { ...TOKENS, access_token: "fresh-acc" } });
    instanceRequest.mockResolvedValue({ data: { ok: true } });
    const onError = responseInterceptors[0];
    const original = { url: "/user/profile", headers: { Authorization: "Bearer expired-acc" } as Record<string, string> };
    const result = await onError({ config: original, response: { status: 401 } });
    expect(result).toEqual({ data: { ok: true } });
    expect(post).toHaveBeenCalledWith(expect.stringMatching(/\/auth\/refresh$/), undefined, expect.objectContaining({ headers: { "X-CSRF-Token": "cookie-csrf" } }));
    expect(instanceRequest).toHaveBeenCalledWith(expect.objectContaining({ _retried: true, headers: expect.objectContaining({ Authorization: "Bearer fresh-acc" }) }));
    expect(useAuthStore.getState().accessToken).toBe("fresh-acc");
    expect(useAuthStore.getState().notice).toBeNull();   // the user noticed nothing
  });

  it("when the refresh itself fails on 401 the user is signed out with the session-expired notice", async () => {
    useAuthStore.getState().setTokens(TOKENS);
    post.mockRejectedValue({ response: { status: 401 } });
    const onError = responseInterceptors[0];
    await expect(onError({ config: { url: "/user/profile", headers: {} }, response: { status: 401 } })).rejects.toBeTruthy();
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(useAuthStore.getState().notice).toBe(SESSION_EXPIRED_NOTICE);
  });
});

describe("route guards wait for the session bootstrap", () => {
  function mount(path: string) {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<ProtectedRoute />}><Route path="/dashboard" element={<p>dashboard</p>} /></Route>
          <Route element={<PublicOnlyRoute />}><Route path="/login" element={<p>login page</p>} /></Route>
        </Routes>
      </MemoryRouter>);
  }

  it("shows a loader while unknown, then the page or the login redirect", () => {
    useAuthStore.setState({ status: "unknown", accessToken: null });
    const { unmount } = mount("/dashboard");
    expect(screen.queryByText("dashboard")).toBeNull();
    expect(screen.queryByText("login page")).toBeNull();
    unmount();
    useAuthStore.setState({ status: "anonymous", accessToken: null });
    const second = mount("/dashboard");
    expect(screen.getByText("login page")).toBeInTheDocument();
    second.unmount();
    useAuthStore.setState({ status: "authenticated", accessToken: "acc" });
    mount("/dashboard");
    expect(screen.getByText("dashboard")).toBeInTheDocument();
  });
});

describe("refresh rate limit (429) never signs the user out", () => {
  beforeEach(() => {
    document.cookie = "autobuy_csrf=cookie-csrf; path=/";
    useAuthStore.setState({ accessToken: null, csrfToken: null, user: null, status: "unknown", notice: null });
    post.mockReset();
    vi.useFakeTimers();
  });
  afterEach(() => { vi.useRealTimers(); });

  it("bootstrap retries a 429 with backoff and keeps the session once it succeeds", async () => {
    post.mockRejectedValueOnce({ response: { status: 429, headers: {} } })
        .mockRejectedValueOnce({ response: { status: 429, headers: { "retry-after": "2" } } })
        .mockResolvedValue({ data: TOKENS });
    const p = bootstrapSession();
    await vi.advanceTimersByTimeAsync(1000);   // first backoff step
    await vi.advanceTimersByTimeAsync(2000);   // Retry-After honoured
    expect(await p).toBe(true);
    expect(post).toHaveBeenCalledTimes(3);
    expect(useAuthStore.getState().accessToken).toBe("acc-123");
    expect(useAuthStore.getState().notice).toBeNull();
    expect(document.cookie).toContain("autobuy_csrf=cookie-csrf");
  });

  it("a 429 that outlasts the one-minute budget leaves the cookies in place and says so, instead of 'session expired'", async () => {
    post.mockRejectedValue({ response: { status: 429, headers: {} } });
    const p = bootstrapSession();
    await vi.advanceTimersByTimeAsync(31_000);
    expect(await p).toBe(false);
    expect(post).toHaveBeenCalledTimes(6);   // first try + 1 s, 2 s, 4 s, 8 s, 16 s
    const s = useAuthStore.getState();
    expect(s.status).toBe("anonymous");
    expect(s.notice).toBe(RATE_LIMITED_NOTICE);
    expect(document.cookie).toContain("autobuy_csrf=cookie-csrf");   // the next load simply works
  });

  it("a 401 on any request whose refresh is rate limited fails that request but keeps the session", async () => {
    useAuthStore.getState().setTokens(TOKENS);
    post.mockRejectedValue({ response: { status: 429, headers: { "retry-after": "60" } } });
    const onError = responseInterceptors[0];
    const pending = onError({ config: { url: "/user/profile", headers: {} }, response: { status: 401 } });
    const settled = pending.then(() => "resolved", (e: unknown) => e);
    await vi.advanceTimersByTimeAsync(60_000);   // one Retry-After wait uses the whole budget; the second 429 is final
    const outcome = await settled;
    expect(post).toHaveBeenCalledTimes(2);
    expect((outcome as { response: { status: number } }).response.status).toBe(429);
    expect(useAuthStore.getState().accessToken).toBe("acc-123");
    expect(useAuthStore.getState().notice).toBeNull();
    expect(document.cookie).toContain("autobuy_csrf=cookie-csrf");
  });

  it("any other refresh failure is still a real sign-out", async () => {
    post.mockRejectedValue({ response: { status: 401 } });
    const p = bootstrapSession();
    await vi.advanceTimersByTimeAsync(0);
    expect(await p).toBe(false);
    expect(useAuthStore.getState().notice).toBe(SESSION_EXPIRED_NOTICE);
    expect(document.cookie).not.toContain("autobuy_csrf=cookie-csrf");
  });
});
