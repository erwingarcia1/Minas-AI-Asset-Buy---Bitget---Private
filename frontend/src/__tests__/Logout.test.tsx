import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Navbar } from "@/components/Layout/Navbar";
import { useAuthStore } from "@/store/authStore";

vi.mock("@/api/endpoints", () => ({
  authAPI: { logout: vi.fn(), login: vi.fn(), register: vi.fn() },
  userAPI: { profile: vi.fn() },
}));
vi.mock("@/api/client", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/client")>()), refreshAccessToken: vi.fn() }));
import { authAPI } from "@/api/endpoints";
import { refreshAccessToken } from "@/api/client";

const USER = { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z" };

describe("Sign out never pretends (issue #64)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: USER, status: "authenticated", accessToken: "a", csrfToken: "c" } as never);
  });

  it("clears the session only after the server confirmed", async () => {
    vi.mocked(authAPI.logout).mockResolvedValue(undefined as never);
    render(<MemoryRouter><Navbar onToggleSidebar={() => undefined} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(useAuthStore.getState().user).toBeNull());
    expect(authAPI.logout).toHaveBeenCalledTimes(1);
    expect(refreshAccessToken).not.toHaveBeenCalled();
  });

  it("retries once with a refreshed CSRF value when the first attempt is refused", async () => {
    vi.mocked(authAPI.logout).mockRejectedValueOnce(new Error("403")).mockResolvedValueOnce(undefined as never);
    vi.mocked(refreshAccessToken).mockResolvedValue("fresh-token");
    render(<MemoryRouter><Navbar onToggleSidebar={() => undefined} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(useAuthStore.getState().user).toBeNull());
    expect(refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(authAPI.logout).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("sign-out-error")).toBeNull();
  });

  it("shows the failure and keeps the user signed in when the server refuses twice", async () => {
    vi.mocked(authAPI.logout).mockRejectedValue(new Error("network"));
    vi.mocked(refreshAccessToken).mockRejectedValue(new Error("network"));
    render(<MemoryRouter><Navbar onToggleSidebar={() => undefined} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(screen.getByTestId("sign-out-error")).toHaveTextContent("Sign-out failed, try again."));
    expect(useAuthStore.getState().user).toEqual(USER);
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(authAPI.logout).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
  });
});
