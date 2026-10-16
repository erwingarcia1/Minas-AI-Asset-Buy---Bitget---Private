import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SecuritySettings from "@/components/Settings/SecuritySettings";

vi.mock("qrcode", () => ({ default: { toString: vi.fn().mockResolvedValue("<svg data-testid=\"qr-svg\"></svg>") } }));
vi.mock("@/api/endpoints", () => ({
  twoFactorAPI: { status: vi.fn(), enrol: vi.fn(), confirm: vi.fn(), regenerateCodes: vi.fn(), disable: vi.fn() },
}));
vi.mock("@/api/client", () => ({ errorMessage: (e: unknown) => (e instanceof Error ? e.message : "Request failed") }));
const signOutEverywhere = vi.fn().mockResolvedValue(undefined);
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ signOutEverywhere }) }));
import { twoFactorAPI } from "@/api/endpoints";

const OFF = { enabled: false, enrolled_at: null, recovery_codes_remaining: 0, pending_enrolment: false };
const ON = { enabled: true, enrolled_at: "2026-10-13T10:00:00Z", recovery_codes_remaining: 8, pending_enrolment: false };
const CODES = Array.from({ length: 10 }, (_, i) => `AAAA-BBBB-00${i}`);

function mount() {
  return render(<MemoryRouter><SecuritySettings /></MemoryRouter>);
}

describe("Security settings: two-factor authentication", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("enrols: password, QR plus secret, code, then the recovery codes shown once", async () => {
    vi.mocked(twoFactorAPI.status).mockResolvedValueOnce(OFF as never).mockResolvedValue({ ...ON, recovery_codes_remaining: 10 } as never);
    vi.mocked(twoFactorAPI.enrol).mockResolvedValue({ secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP" } as never);
    vi.mocked(twoFactorAPI.confirm).mockResolvedValue({ recovery_codes: CODES } as never);
    mount();
    expect(await screen.findByTestId("two-factor-status")).toHaveTextContent("Two-factor authentication: off");
    fireEvent.click(screen.getByRole("button", { name: "Set up two-factor authentication" }));
    const pw = screen.getByLabelText("Current password");
    expect(pw).toHaveAttribute("type", "password");
    fireEvent.change(pw, { target: { value: "Sess-Passw0rd!1" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(twoFactorAPI.enrol).toHaveBeenCalledWith("Sess-Passw0rd!1"));
    expect(await screen.findByTestId("totp-secret")).toHaveTextContent("JBSWY3DPEHPK3PXP");
    await screen.findByTestId("qr");
    fireEvent.change(screen.getByLabelText("Code from the app"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(twoFactorAPI.confirm).toHaveBeenCalledWith("123456"));
    const codes = await screen.findByTestId("recovery-codes");
    expect(codes.querySelectorAll("li")).toHaveLength(10);
    expect(codes).toHaveTextContent("AAAA-BBBB-000");
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("shown only once");
    fireEvent.click(screen.getByRole("button", { name: "I have saved them" }));
    await waitFor(() => expect(screen.queryByTestId("recovery-codes")).not.toBeInTheDocument());
    expect(screen.getByTestId("two-factor-status")).toHaveTextContent("Two-factor authentication: on");
  });

  it("a wrong code during enrolment shows the message and keeps the step", async () => {
    vi.mocked(twoFactorAPI.status).mockResolvedValue(OFF as never);
    vi.mocked(twoFactorAPI.enrol).mockResolvedValue({ secret: "S", otpauth_uri: "otpauth://totp/x?secret=S" } as never);
    vi.mocked(twoFactorAPI.confirm).mockRejectedValue(new Error("That code was not accepted. Scan the QR code again and enter a fresh code."));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Set up two-factor authentication" }));
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.change(await screen.findByLabelText("Code from the app"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That code was not accepted");
    expect(screen.getByTestId("enrol-confirm")).toBeInTheDocument();
  });

  it("when on: shows codes left, regenerates against a code, and turning off needs password plus a factor", async () => {
    vi.mocked(twoFactorAPI.status).mockResolvedValue(ON as never);
    vi.mocked(twoFactorAPI.regenerateCodes).mockResolvedValue({ recovery_codes: CODES } as never);
    vi.mocked(twoFactorAPI.disable).mockResolvedValue(undefined as never);
    mount();
    expect(await screen.findByTestId("two-factor-status")).toHaveTextContent("8 recovery codes left");
    fireEvent.click(screen.getByRole("button", { name: "New recovery codes" }));
    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate new codes" }));
    await waitFor(() => expect(twoFactorAPI.regenerateCodes).toHaveBeenCalledWith("654321"));
    await screen.findByTestId("recovery-codes");
    fireEvent.click(screen.getByRole("button", { name: "I have saved them" }));

    fireEvent.click(await screen.findByRole("button", { name: "Turn off two-factor" }));
    const form = screen.getByTestId("disable-form");
    expect(form).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn off two-factor" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    expect(screen.getByLabelText("Recovery code")).toHaveAttribute("maxlength", "23");   // XXXXX-XXXXX-XXXXX-XXXXX fits; the 14-character codes below still work
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "AAAA-BBBB-0001" } });
    fireEvent.click(screen.getByRole("button", { name: "Turn off two-factor" }));
    await waitFor(() => expect(twoFactorAPI.disable).toHaveBeenCalledWith("pw", { recovery_code: "AAAA-BBBB-0001" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Two-factor authentication is off");
  });

  it("sign out everywhere asks first, then calls through", async () => {
    vi.mocked(twoFactorAPI.status).mockResolvedValue(OFF as never);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    expect(signOutEverywhere).not.toHaveBeenCalled();
    expect(screen.getByTestId("sign-out-everywhere")).toHaveTextContent("this one included");
    fireEvent.click(screen.getAllByRole("button", { name: "Sign out everywhere" }).at(-1)!);
    await waitFor(() => expect(signOutEverywhere).toHaveBeenCalledTimes(1));
  });

  it("never renders a password field until the user starts an action", async () => {
    vi.mocked(twoFactorAPI.status).mockResolvedValue(OFF as never);
    const { container } = mount();
    await screen.findByTestId("two-factor-status");
    expect(container.querySelectorAll("input[type=password]")).toHaveLength(0);
  });
});
