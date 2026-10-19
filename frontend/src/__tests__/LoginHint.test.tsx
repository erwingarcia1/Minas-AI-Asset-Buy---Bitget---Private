import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Login from "@/pages/Login";
import { useAuthStore } from "@/store/authStore";

vi.mock("@/api/endpoints", () => ({
  authAPI: { login: vi.fn(), loginMfa: vi.fn() },
  userAPI: { profile: vi.fn() },
  publicAPI: { meta: vi.fn() },
}));
import { publicAPI } from "@/api/endpoints";

const META = { coins: ["BTC"], exchanges: ["bitget"], support_email: "support@example.com", server_ip: "203.0.113.7" };
const HINT = "Coming from Minas AI Guardian? Your account moved with you. Use “Forgot password” to set a password for AutoBuy.";

function mount() {
  return render(<MemoryRouter initialEntries={["/login"]}><Routes><Route path="/login" element={<Login />} /></Routes></MemoryRouter>);
}

/** Let the meta promise settle and React apply the result before asserting that nothing appeared. */
async function settled() {
  await waitFor(() => expect(publicAPI.meta).toHaveBeenCalledTimes(1));
  await act(async () => { await Promise.resolve(); });
}

describe("Login: the Guardian migration hint", () => {
  beforeEach(() => { vi.clearAllMocks(); useAuthStore.getState().setAnonymous(); });

  it("shows the static hint above the form when the public meta says the flag is on", async () => {
    vi.mocked(publicAPI.meta).mockResolvedValue({ ...META, guardian_enabled: true } as never);
    mount();
    const hint = await screen.findByTestId("guardian-login-hint");
    expect(hint).toHaveTextContent(HINT);
    const email = screen.getByLabelText("Email");
    expect(hint.compareDocumentPosition(email) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();   // above the form
    expect(screen.getByRole("link", { name: "Forgot password?" })).toBeInTheDocument();           // the path the hint names
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it.each([["false", { ...META, guardian_enabled: false }], ["missing (older API)", META]])("shows nothing when the flag is %s", async (_label, meta) => {
    vi.mocked(publicAPI.meta).mockResolvedValue(meta as never);
    mount();
    await settled();
    expect(screen.queryByTestId("guardian-login-hint")).toBeNull();
    expect(screen.queryByText(/Minas AI Guardian/)).toBeNull();
  });

  it("shows nothing when the meta call fails, and the sign-in form is untouched", async () => {
    vi.mocked(publicAPI.meta).mockRejectedValue(new Error("offline"));
    mount();
    await settled();
    expect(screen.queryByTestId("guardian-login-hint")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });
});
