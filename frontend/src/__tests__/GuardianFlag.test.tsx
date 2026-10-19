import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FeatureRoute } from "@/components/Common/FeatureRoute";
import { Sidebar } from "@/components/Layout/Sidebar";
import Settings from "@/pages/Settings";

vi.mock("@/api/endpoints", () => ({
  authAPI: { changePassword: vi.fn() },
  userAPI: { exchangeKeys: vi.fn().mockResolvedValue([]), addExchangeKey: vi.fn(), verifyExchangeKey: vi.fn(), deleteExchangeKey: vi.fn(), sendVerification: vi.fn(),
             supportedExchanges: vi.fn().mockResolvedValue(["bitget"]), updateProfile: vi.fn() },
  reportsAPI: { performance: vi.fn().mockResolvedValue({ has_data: false }) },
  publicAPI: { meta: vi.fn().mockResolvedValue({ coins: ["BTC"], exchanges: ["bitget"], support_email: "support@example.com", server_ip: "203.0.113.7" }) },
  notificationAPI: { preferences: vi.fn().mockResolvedValue({ email_enabled: true, email_purchase_confirmations: true, email_weekly_summary: true, email_milestones: true,
    email_low_balance: true, always_on_emails: ["password reset"], sms_enabled: false, push_enabled: true, quiet_hours_start: 21, quiet_hours_end: 8, phone_number: null, has_device_token: false }), update: vi.fn() },
  integrationsAPI: { available: vi.fn().mockResolvedValue([]), webhooks: vi.fn().mockResolvedValue([]) },
  twoFactorAPI: { status: vi.fn().mockResolvedValue({ enabled: false, enrolled_at: null, recovery_codes_remaining: 0, pending_enrolment: false }),
                  enrol: vi.fn(), confirm: vi.fn(), regenerateCodes: vi.fn(), disable: vi.fn() },
  guardianAPI: { accounts: vi.fn().mockResolvedValue([]), account: vi.fn(), addAccount: vi.fn(), replaceKey: vi.fn(), updateAccount: vi.fn(), disable: vi.fn(), enable: vi.fn(), remove: vi.fn() },
}));
let currentUser: Record<string, unknown> | null = null;   // null = the profile has not arrived yet
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: currentUser, logout: vi.fn(), signOutEverywhere: vi.fn(), refreshProfile: vi.fn() }) }));
import { guardianAPI } from "@/api/endpoints";

const USER = { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z" };

describe("Guardian flag: the sidebar entry", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it.each([["missing (older API)", {}], ["false", { guardian_enabled: false }]])("is hidden when guardian_enabled is %s", (_label, extra) => {
    currentUser = { ...USER, ...extra };
    render(<MemoryRouter initialEntries={["/dashboard"]}><Sidebar open onClose={() => undefined} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Guardian" })).toBeNull();
  });

  it("is shown when true, points at the monitoring section, and is lit there instead of Settings", () => {
    currentUser = { ...USER, guardian_enabled: true };
    render(<MemoryRouter initialEntries={["/settings/monitoring"]}><Sidebar open onClose={() => undefined} /></MemoryRouter>);
    const guardian = screen.getByRole("link", { name: "Guardian" });
    expect(guardian).toHaveAttribute("href", "/settings/monitoring");
    expect(guardian.className).toMatch(/bg-brand-50/);
    expect(screen.getByRole("link", { name: "Settings" }).className).not.toMatch(/bg-brand-50/);
    expect(screen.queryByRole("link", { name: "Admin" })).toBeNull();   // the role still decides that one
  });

  it("Settings stays lit on its own pages", () => {
    currentUser = { ...USER, guardian_enabled: true };
    render(<MemoryRouter initialEntries={["/settings/security"]}><Sidebar open onClose={() => undefined} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Settings" }).className).toMatch(/bg-brand-50/);
    expect(screen.getByRole("link", { name: "Guardian" }).className).not.toMatch(/bg-brand-50/);
  });
});

describe("FeatureRoute", () => {
  function mount() {
    return render(
      <MemoryRouter initialEntries={["/settings/monitoring"]}>
        <Routes>
          <Route element={<FeatureRoute />}><Route path="/settings/monitoring" element={<p>monitoring page</p>} /></Route>
          <Route path="/dashboard" element={<p>dashboard page</p>} />
        </Routes>
      </MemoryRouter>);
  }

  it.each([["missing", {}], ["false", { guardian_enabled: false }]])("sends the user to the dashboard when the feature is %s", (_label, extra) => {
    currentUser = { ...USER, ...extra };
    mount();
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
    expect(screen.queryByText("monitoring page")).toBeNull();
  });

  it("renders the child route when the feature is on", () => {
    currentUser = { ...USER, guardian_enabled: true };
    mount();
    expect(screen.getByText("monitoring page")).toBeInTheDocument();
    expect(screen.queryByText("dashboard page")).toBeNull();
  });

  it("waits for the profile on a deep link instead of bouncing", () => {
    currentUser = null;
    mount();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByText("monitoring page")).toBeNull();
    expect(screen.queryByText("dashboard page")).toBeNull();
  });
});

describe("Settings page: the monitoring section follows the flag", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("has no Guardian section and never calls the Guardian API when the flag is off, even on the deep link", async () => {
    currentUser = { ...USER };
    render(<MemoryRouter initialEntries={["/settings/monitoring"]}><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    expect(screen.queryByRole("region", { name: "Guardian: exchange monitoring" })).toBeNull();
    expect(screen.queryByText(/Guardian/)).toBeNull();
    expect(guardianAPI.accounts).not.toHaveBeenCalled();
  });

  it("shows it, highlighted on /settings/monitoring, when the flag is on", async () => {
    currentUser = { ...USER, guardian_enabled: true };
    render(<MemoryRouter initialEntries={["/settings/monitoring"]}><Settings /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Guardian: exchange monitoring" });
    expect(section.className).toMatch(/ring-2/);
    await waitFor(() => expect(guardianAPI.accounts).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("No monitored accounts yet.")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Email notifications" }).className).not.toMatch(/ring-2/);
  });

  it("is not highlighted on plain /settings", async () => {
    currentUser = { ...USER, guardian_enabled: true };
    render(<MemoryRouter initialEntries={["/settings"]}><Settings /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Guardian: exchange monitoring" });
    expect(section.className).not.toMatch(/ring-2/);
  });
});
