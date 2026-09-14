import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
vi.mock("axios", () => {
  const instance = { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } }, request: vi.fn(), post: vi.fn(), get: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: { create: () => instance, post: (...a: unknown[]) => post(...a), isAxiosError: () => false }, AxiosError: class {} };
});
import { bootstrapSession, csrfToken, refreshAccessToken } from "@/api/client";
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

  it("bootstrap without a valid cookie marks the session anonymous", async () => {
    post.mockRejectedValue(new Error("401"));
    expect(await bootstrapSession()).toBe(false);
    expect(useAuthStore.getState().status).toBe("anonymous");
    expect(useAuthStore.getState().accessToken).toBeNull();
    await expect(refreshAccessToken()).rejects.toThrow();
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
