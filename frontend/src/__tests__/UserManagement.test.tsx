import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import UserManagement from "@/components/Admin/UserManagement";

// The real adminAPI over a mocked axios instance: these tests prove what is posted, not only what is called.
vi.mock("@/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  CSRF_HEADER: "X-CSRF-Token",
  csrfToken: () => null,
  errorMessage: (e: unknown, fallback = "Request failed") =>
    (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? (e instanceof Error ? e.message : fallback),
}));
import { api } from "@/api/client";

const ROW = { id: "u1", email: "maria@example.com", role: "user", kyc_status: "verified", suspended: false, created_at: "2026-09-01T00:00:00Z", invested_usdt: 300, totp_enabled: true };
const RESET_URL = "/admin/users/u1/2fa/reset";

async function openResetDialog() {
  render(<UserManagement />);
  expect(await screen.findByTestId("totp-pill")).toHaveTextContent("2FA");
  fireEvent.click(screen.getByRole("button", { name: "Reset 2FA" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("Reset two-factor authentication?");
  return dialog;
}

describe("Admin: two-factor reset asks for the admin's own authenticator code", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockResolvedValue({ data: { items: [ROW], total: 1 } } as never);
    vi.mocked(api.post).mockResolvedValue({ status: 204, data: "" } as never);
  });

  it("offers an optional code field with help text and posts {code} (whitespace stripped) when one is typed", async () => {
    const dialog = await openResetDialog();
    const code = within(dialog).getByLabelText("Your authenticator code");
    expect(code).toHaveAttribute("inputmode", "numeric");
    expect(code).toHaveAttribute("maxlength", "7");
    expect(code).toHaveAttribute("autocomplete", "one-time-code");
    expect(within(dialog).getByText("Needed when two-factor is on for your own account.")).toBeInTheDocument();
    fireEvent.change(code, { target: { value: "123 456" } });
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reset 2FA" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(RESET_URL, { code: "123456" }));
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Two-factor authentication reset for maria@example.com/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("posts an empty body when the field is left blank (an admin whose own account has two-factor off)", async () => {
    const dialog = await openResetDialog();
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reset 2FA" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(RESET_URL, {}));
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it("the confirmation still needs the customer's email; the code alone does not enable the button", async () => {
    const dialog = await openResetDialog();
    fireEvent.change(within(dialog).getByLabelText("Your authenticator code"), { target: { value: "123456" } });
    expect(within(dialog).getByRole("button", { name: "Reset 2FA" })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("shows the server's plain-language refusal inside the dialog and keeps it open, nothing reloaded", async () => {
    vi.mocked(api.post).mockRejectedValueOnce({ response: { status: 400, data: { detail: "Enter the current code from your authenticator app to reset two-factor for another account." } } });
    const dialog = await openResetDialog();
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reset 2FA" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/current code from your authenticator app/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("the code field is cleared between two openings of the dialog", async () => {
    const dialog = await openResetDialog();
    fireEvent.change(within(dialog).getByLabelText("Your authenticator code"), { target: { value: "123456" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reset 2FA" }));
    expect(within(await screen.findByRole("dialog")).getByLabelText("Your authenticator code")).toHaveValue("");
  });
});
