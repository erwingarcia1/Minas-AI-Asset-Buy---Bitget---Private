import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "@/pages/Settings";
import { useAuthStore } from "@/store/authStore";

/** The real useAuth and store here (Settings.test and SecuritySettings.test mock the hook away): turning
 *  two-factor on or off in the Security section must reach the change-email form on the same page. */
vi.mock("qrcode", () => ({ default: { toString: vi.fn().mockResolvedValue("<svg></svg>") } }));
vi.mock("@/api/endpoints", () => ({
  authAPI: { changePassword: vi.fn(), logout: vi.fn(), logoutAll: vi.fn() },
  userAPI: { profile: vi.fn(), exchangeKeys: vi.fn().mockResolvedValue([]), addExchangeKey: vi.fn(), verifyExchangeKey: vi.fn(), deleteExchangeKey: vi.fn(), sendVerification: vi.fn(),
             supportedExchanges: vi.fn().mockResolvedValue(["bitget"]), updateProfile: vi.fn() },
  reportsAPI: { performance: vi.fn().mockResolvedValue({ has_data: false }) },
  publicAPI: { meta: vi.fn().mockResolvedValue({ coins: ["BTC"], exchanges: ["bitget"], support_email: "support@example.com", server_ip: "203.0.113.7" }) },
  notificationAPI: { preferences: vi.fn().mockResolvedValue({ email_enabled: true, email_purchase_confirmations: true, email_weekly_summary: true, email_milestones: true,
    email_low_balance: true, always_on_emails: ["password reset"], sms_enabled: false, push_enabled: true, quiet_hours_start: 21, quiet_hours_end: 8, phone_number: null, has_device_token: false }), update: vi.fn() },
  integrationsAPI: { available: vi.fn().mockResolvedValue([]), webhooks: vi.fn().mockResolvedValue([]) },
  twoFactorAPI: { status: vi.fn(), enrol: vi.fn(), confirm: vi.fn(), regenerateCodes: vi.fn(), disable: vi.fn() },
  guardianAPI: { accounts: vi.fn().mockResolvedValue([]), account: vi.fn(), addAccount: vi.fn(), replaceKey: vi.fn(), updateAccount: vi.fn(), disable: vi.fn(), enable: vi.fn(), remove: vi.fn() },
}));
import { twoFactorAPI, userAPI } from "@/api/endpoints";

const OFF = { enabled: false, enrolled_at: null, recovery_codes_remaining: 0, pending_enrolment: false };
const ON = { enabled: true, enrolled_at: "2026-10-19T10:00:00Z", recovery_codes_remaining: 10, pending_enrolment: false };
const PROFILE = { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", mfa_enabled: false };

describe("Settings: the change-email form follows two-factor without a new sign-in", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ accessToken: "acc", csrfToken: "csrf", status: "authenticated", user: { ...PROFILE } as never });   // the login-time snapshot
  });
  afterEach(() => { act(() => useAuthStore.setState({ accessToken: null, csrfToken: null, status: "unknown", user: null })); });   // the page is still mounted here

  it("asks for an authenticator code as soon as two-factor is on, and stops once it is off", async () => {
    vi.mocked(twoFactorAPI.status).mockResolvedValueOnce(OFF as never).mockResolvedValueOnce(ON as never).mockResolvedValue(OFF as never);
    vi.mocked(twoFactorAPI.enrol).mockResolvedValue({ secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP" } as never);
    vi.mocked(twoFactorAPI.confirm).mockResolvedValue({ recovery_codes: ["AAAAA-BBBBB-CCCCC-00001"] } as never);
    vi.mocked(twoFactorAPI.disable).mockResolvedValue(undefined as never);
    vi.mocked(userAPI.profile).mockResolvedValueOnce({ ...PROFILE, mfa_enabled: true } as never).mockResolvedValueOnce({ ...PROFILE, mfa_enabled: false } as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    const emailForm = await screen.findByTestId("change-email-form");
    expect(within(emailForm).queryByLabelText("Authenticator code")).toBeNull();
    expect(screen.getByText(/so the change needs your current password\.$/)).toBeInTheDocument();

    // two-factor goes on in the Security section
    const security = screen.getByRole("region", { name: "Security" });
    fireEvent.click(await within(security).findByRole("button", { name: "Set up two-factor authentication" }));
    fireEvent.change(within(security).getByLabelText("Current password"), { target: { value: "Sess-Passw0rd!1" } });
    fireEvent.click(within(security).getByRole("button", { name: "Continue" }));
    fireEvent.change(await within(security).findByLabelText("Code from the app"), { target: { value: "123456" } });
    fireEvent.click(within(security).getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(userAPI.profile).toHaveBeenCalledTimes(1));
    expect(await within(emailForm).findByLabelText("Authenticator code")).toBeInTheDocument();   // the form follows, no new sign-in
    expect(screen.getByText(/current password and an authenticator code\.$/)).toBeInTheDocument();
    expect(useAuthStore.getState().user?.mfa_enabled).toBe(true);
    expect(within(emailForm).getByRole("button", { name: "Update email" })).toBeDisabled();   // the code is required before the request goes out

    // and off again
    fireEvent.click(within(security).getByRole("button", { name: "I have saved them" }));
    fireEvent.click(await within(security).findByRole("button", { name: "Turn off two-factor" }));
    fireEvent.change(within(security).getByLabelText("Current password"), { target: { value: "Sess-Passw0rd!1" } });
    fireEvent.change(within(security).getByLabelText("Authenticator code"), { target: { value: "654321" } });
    fireEvent.click(within(security).getByRole("button", { name: "Turn off two-factor" }));
    await waitFor(() => expect(userAPI.profile).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(emailForm).queryByLabelText("Authenticator code")).toBeNull());
    expect(screen.getByText(/so the change needs your current password\.$/)).toBeInTheDocument();
    expect(useAuthStore.getState().user?.mfa_enabled).toBe(false);
  });
});
