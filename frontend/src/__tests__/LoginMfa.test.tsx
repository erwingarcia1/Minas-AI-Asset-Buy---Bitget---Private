import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Login from "@/pages/Login";
import { useAuthStore } from "@/store/authStore";

vi.mock("@/api/endpoints", () => ({
  authAPI: { login: vi.fn(), loginMfa: vi.fn() },
  userAPI: { profile: vi.fn() },
}));
vi.mock("@/api/client", () => ({
  errorMessage: (e: unknown) => (e instanceof Error ? e.message : "Request failed"),
  refreshAccessToken: vi.fn(),
}));
import { authAPI, userAPI } from "@/api/endpoints";

const TOKENS = { access_token: "acc", token_type: "bearer", expires_in: 1800, csrf_token: "csrf" };
const PROFILE = { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z" };

function mount() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/dashboard" element={<p>dashboard page</p>} />
      </Routes>
    </MemoryRouter>);
}

async function submitPassword() {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "me@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Sess-Passw0rd!1" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("Login with a second factor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.getState().setAnonymous();
    vi.mocked(userAPI.profile).mockResolvedValue(PROFILE as never);
    vi.mocked(authAPI.loginMfa).mockResolvedValue(TOKENS as never);
  });

  it("a plain login still goes straight to the app", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "session", tokens: TOKENS } as never);
    mount();
    await submitPassword();
    await screen.findByText("dashboard page");
    expect(useAuthStore.getState().accessToken).toBe("acc");
  });

  it("a 202 shows the code step without a session and without fetching the profile", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "mfa", mfaToken: "pending-1", expiresIn: 300 } as never);
    mount();
    await submitPassword();
    expect(await screen.findByTestId("mfa-form")).toBeInTheDocument();
    expect(screen.getByText("Enter your code")).toBeInTheDocument();
    expect(userAPI.profile).not.toHaveBeenCalled();
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(screen.queryByText("dashboard page")).not.toBeInTheDocument();
  });

  it("the code finishes the sign-in and lands in the app", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "mfa", mfaToken: "pending-1", expiresIn: 300 } as never);
    mount();
    await submitPassword();
    fireEvent.change(await screen.findByLabelText("Authenticator code"), { target: { value: "123 456" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("dashboard page");
    expect(authAPI.loginMfa).toHaveBeenCalledWith("pending-1", { code: "123456" });
    expect(useAuthStore.getState().accessToken).toBe("acc");
    expect(useAuthStore.getState().user).toEqual(PROFILE);
  });

  it("refuses anything but six digits before calling the API", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "mfa", mfaToken: "pending-1", expiresIn: 300 } as never);
    mount();
    await submitPassword();
    fireEvent.change(await screen.findByLabelText("Authenticator code"), { target: { value: "12ab" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the six-digit code");
    expect(authAPI.loginMfa).not.toHaveBeenCalled();
  });

  it("a recovery code is sent as such", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "mfa", mfaToken: "pending-1", expiresIn: 300 } as never);
    mount();
    await submitPassword();
    fireEvent.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "aaaa-bbbb-cccc" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("dashboard page");
    expect(authAPI.loginMfa).toHaveBeenCalledWith("pending-1", { recovery_code: "aaaa-bbbb-cccc" });
  });

  it("a rejected code shows the server's message and stays on the step", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "mfa", mfaToken: "pending-1", expiresIn: 300 } as never);
    vi.mocked(authAPI.loginMfa).mockRejectedValue(new Error("That code was not accepted. Check the time on your phone and enter the current code."));
    mount();
    await submitPassword();
    fireEvent.change(await screen.findByLabelText("Authenticator code"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That code was not accepted");
    expect(screen.getByTestId("mfa-form")).toBeInTheDocument();
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("back returns to the password form and forgets the pending token", async () => {
    vi.mocked(authAPI.login).mockResolvedValue({ kind: "mfa", mfaToken: "pending-1", expiresIn: 300 } as never);
    mount();
    await submitPassword();
    fireEvent.click(await screen.findByRole("button", { name: "Back to sign in" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument());
    expect(screen.queryByTestId("mfa-form")).not.toBeInTheDocument();
  });
});
