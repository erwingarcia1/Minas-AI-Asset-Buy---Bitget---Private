import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "@/pages/Settings";

vi.mock("@/api/endpoints", () => ({
  authAPI: { changePassword: vi.fn() },
  userAPI: { exchangeKeys: vi.fn(), addExchangeKey: vi.fn(), verifyExchangeKey: vi.fn(), deleteExchangeKey: vi.fn(), sendVerification: vi.fn(),
             supportedExchanges: vi.fn().mockResolvedValue(["bitget"]) },
  reportsAPI: { performance: vi.fn().mockResolvedValue({ has_data: false }) },
  publicAPI: { meta: vi.fn().mockResolvedValue({ coins: ["BTC"], exchanges: ["bitget"], support_email: "support@example.com", server_ip: "203.0.113.7" }) },
  notificationAPI: { preferences: vi.fn(), update: vi.fn() },
  integrationsAPI: { available: vi.fn().mockResolvedValue([]), webhooks: vi.fn().mockResolvedValue([]) },
}));
const authUser: Record<string, unknown> = { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", withdrawals_allowed: true };
let currentUser: Record<string, unknown> | null = authUser;   // null = session still bootstrapping (a direct load of /settings)
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: currentUser, logout: vi.fn(), refreshProfile: vi.fn().mockResolvedValue(authUser) }) }));
import { notificationAPI, userAPI } from "@/api/endpoints";

const PREFS = { email_enabled: true, email_purchase_confirmations: true, email_weekly_summary: true, email_milestones: true, email_low_balance: true,
  always_on_emails: ["password reset", "password changed", "purchase failed", "withdrawal failed", "withdrawal needs information"],
  sms_enabled: false, push_enabled: true, quiet_hours_start: 21, quiet_hours_end: 8, phone_number: null, has_device_token: false };

const FAILED = { id: "k1", exchange: "bitget", api_key_masked: "bg_a...z9", verified: false, verification_status: "failed",
  verification_error: "Bitget rejected this server's IP address (error 40018): Invalid IP. Add the server's IP to the key's allowlist, or create the key without an IP restriction.",
  last_checked_at: "2026-09-11T11:38:00Z", created_at: "2026-09-11T11:37:59Z" };
const VERIFIED_WITHDRAW = { ...FAILED, id: "k2", exchange: "binance", verified: true, verification_status: "verified", verification_error: null,
  verified_at: "2026-09-11T11:40:00Z", withdrawal_enabled: true };

describe("Settings: exchange keys", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(notificationAPI.preferences).mockResolvedValue(PREFS as never); });

  it("shows a failed key with its readable reason and a retry button, never 'pending' forever", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([FAILED] as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    const row = await screen.findByTestId("exchange-key");
    expect(row).toHaveTextContent("failed");
    expect(row).not.toHaveTextContent("pending");
    expect(screen.getByRole("alert")).toHaveTextContent(/rejected this server's IP address \(error 40018\)/);
    expect(screen.getByRole("button", { name: "Retry verification" })).toBeInTheDocument();
  });

  it("retry calls the verify endpoint, shows the outcome and reloads the list", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValueOnce([FAILED] as never)
      .mockResolvedValue([{ ...FAILED, verified: true, verification_status: "verified", verification_error: null, verified_at: "2026-09-11T11:45:00Z", withdrawal_enabled: false }] as never);
    vi.mocked(userAPI.verifyExchangeKey).mockResolvedValue({ verified: true, result: "ok", detail: "Key verified on Bitget: spot trading enabled",
      withdrawal_permission_warning: false, verification_status: "verified" } as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Retry verification" }));
    await waitFor(() => expect(userAPI.verifyExchangeKey).toHaveBeenCalledWith("k1"));
    await screen.findByText("Key verified on Bitget: spot trading enabled");
    await waitFor(() => expect(screen.getByTestId("exchange-key")).toHaveTextContent("verified"));
    expect(screen.queryByRole("button", { name: /Retry verification|Verify now/ })).not.toBeInTheDocument();
  });

  it("warns when a verified key can withdraw", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([VERIFIED_WITHDRAW] as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByTestId("exchange-key");
    expect(screen.getByRole("status")).toHaveTextContent(/This key can withdraw funds/);
  });

  it("adding a key shows the verification outcome instead of swallowing it", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    vi.mocked(userAPI.addExchangeKey).mockResolvedValue({ ...FAILED, verification_status: "pending", verification_error: null } as never);
    vi.mocked(userAPI.verifyExchangeKey).mockResolvedValue({ verified: false, result: "invalid_key",
      detail: "Bitget rejected the credentials (error 40012): apikey/password is incorrect. Check the key, secret, passphrase and, on Binance, the IP allowlist.",
      withdrawal_permission_warning: false, verification_status: "failed" } as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "bg_key_1234567890" } });
    fireEvent.change(screen.getByLabelText("API secret"), { target: { value: "secret-1234567890" } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(userAPI.verifyExchangeKey).toHaveBeenCalledWith("k1"));
    await screen.findByText(/rejected the credentials \(error 40012\)/);
  });
});

describe("Settings: exchange key fields stay out of autofill and password managers", () => {
  it("uses masked text inputs with anti-autofill attributes, no form, and leaves the password form alone", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    const { container } = render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    const key = screen.getByLabelText("API key");
    const secret = screen.getByLabelText("API secret");
    const phrase = screen.getByLabelText("Passphrase (Bitget/OKX)");
    for (const el of [key, secret, phrase]) {
      expect(el).toHaveAttribute("type", "text");
      expect(el).toHaveAttribute("autocomplete", "one-time-code");
      expect(el).toHaveAttribute("data-lpignore", "true");
      expect(el).toHaveAttribute("data-1p-ignore");
      expect(el).toHaveAttribute("data-form-type", "other");
      expect(el.closest("form")).toBeNull();
      expect(el.id).not.toMatch(/email|user|login|password|pass$|secret/i);
      expect(el.getAttribute("name")).not.toMatch(/email|user|login|password|pass$|secret/i);
    }
    expect(secret).toHaveClass("secret-masked");
    expect(screen.getByRole("button", { name: "Show API secret" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save key" })).toHaveAttribute("type", "button");
    expect(container.querySelectorAll('input[type="password"]')).toHaveLength(2);
    // the change-password form is untouched: real password fields with proper autocomplete, inside a form
    const current = container.querySelector('input[autocomplete="current-password"]') as HTMLInputElement;
    const next = container.querySelector('input[autocomplete="new-password"]') as HTMLInputElement;
    expect(current).toHaveAttribute("type", "password");
    expect(next).toHaveAttribute("type", "password");
    expect(current.closest("form")).not.toBeNull();
  });
});

describe("Settings: direct page load", () => {
  it("renders once the session bootstrap delivers the user (hooks keep their order across the early Loading return)", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    currentUser = null;
    const { rerender } = render(<MemoryRouter><Settings /></MemoryRouter>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    currentUser = authUser;
    rerender(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    expect(screen.queryByText(/Something went wrong|Rendered more hooks/)).toBeNull();
  });
});

describe("Settings: unconfirmed withdrawal permission", () => {
  it("warns that the key may be able to withdraw when the exchange did not confirm otherwise", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([{ ...VERIFIED_WITHDRAW, withdrawal_enabled: null }] as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByTestId("exchange-key");
    expect(screen.getByRole("status")).toHaveTextContent(/could not be confirmed.*treat it as able to withdraw/);
  });
});

describe("Settings: extra permissions", () => {
  it("warns, without blocking, when a verified key carries permissions AutoBuy does not need", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([{ ...FAILED, verified: true, verification_status: "verified", verification_error: null,
      verified_at: "2026-09-11T11:45:00Z", withdrawal_enabled: false, permissions: ["taxw", "p2pw", "wtow", "stow", "smow"],
      extra_permissions: ["p2pw", "smow", "taxw", "wtow"] }] as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByTestId("exchange-key");
    expect(screen.getByTestId("extra-permissions")).toHaveTextContent(/more permissions than AutoBuy needs \(P2P → Search, Margin → Trade, Taxation → Search, Wallet → Transfer \(internal\)\)\. For safety, use a key with only Spot → Trade\./);
    expect(screen.queryByRole("status")).toBeNull();          // withdrawal_enabled=false: no withdrawal warning
    expect(screen.queryByRole("alert")).toBeNull();           // nothing blocking
    expect(screen.queryByRole("button", { name: /Verify now|Retry verification/ })).toBeNull();
  });

  it("shows no extra-permissions note for a Spot → Trade only key", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([{ ...FAILED, verified: true, verification_status: "verified", verification_error: null,
      verified_at: "2026-09-11T11:45:00Z", withdrawal_enabled: false, permissions: ["stow"], extra_permissions: [] }] as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByTestId("exchange-key");
    expect(screen.queryByTestId("extra-permissions")).toBeNull();
  });
});

describe("Settings: Bitget guidance", () => {
  it("offers only the exchanges the deployment supports and explains the key permissions, IP whitelist and address book", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    await waitFor(() => expect(Array.from((screen.getByLabelText("Exchange") as HTMLSelectElement).options).map((o) => o.value)).toEqual(["bitget"]));
    const g = screen.getByTestId("key-guidance");
    expect(g).toHaveTextContent(/Read.*Spot trade.*Leave Withdraw off/);
    expect(g).toHaveTextContent(/Whitelist our server's IP/);
    await waitFor(() => expect(screen.getByTestId("server-ip")).toHaveTextContent("203.0.113.7"));   // from /api/public/meta, never hardcoded
    expect(screen.getByRole("button", { name: "Copy IP 203.0.113.7" })).toBeInTheDocument();
    expect(g).toHaveTextContent(/Bitget address book/);
    expect(screen.getByTestId("fee-note")).toHaveTextContent("Fees: Bitget's trading fee applies, taken from the coin you receive. AutoBuy adds no fee of its own.");
    expect(g.textContent).not.toMatch(/\d+\.\d+%/);
  });
});

describe("Settings: private beta, withdrawals not allowed", () => {
  it("drops the withdraw guidance, keeps the IP whitelist step, and shows the beta line", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    authUser.withdrawals_allowed = false;   // the mocked useAuth reads this object
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    const g = screen.getByTestId("key-guidance");
    expect(g).toHaveTextContent(/Read.*Spot trade.*Leave Withdraw off/);
    expect(g).not.toHaveTextContent(/Withdraw immediately/);
    expect(g).not.toHaveTextContent(/Bitget address book/);
    expect(g).toHaveTextContent(/Whitelist our server's IP/);
    expect(screen.getByTestId("withdraw-unavailable")).toHaveTextContent("Sending to your own wallet is coming later. For now, coins stay in your Bitget sub-account.");
    authUser.withdrawals_allowed = true;
  });
});

describe("Settings: email notifications", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    vi.mocked(notificationAPI.preferences).mockResolvedValue(PREFS as never);
    vi.mocked(notificationAPI.update).mockImplementation(async (body) => ({ ...PREFS, ...body }) as never);
  });

  it("lets the user switch optional emails off and lists the always-on ones", async () => {
    render(<MemoryRouter><Settings /></MemoryRouter>);
    const weekly = await screen.findByLabelText(/Weekly summary/);
    expect(weekly).toBeChecked();
    expect(screen.getByTestId("always-on")).toHaveTextContent(/password reset, password changed, purchase failed, withdrawal failed/);
    expect(screen.queryByLabelText(/purchase failed|password/i)).toBeNull();   // no toggle for always-on emails
    fireEvent.click(weekly);
    await waitFor(() => expect(notificationAPI.update).toHaveBeenCalledWith({ email_weekly_summary: false }));
    await waitFor(() => expect(screen.getByLabelText(/Weekly summary/)).not.toBeChecked());
    expect(screen.getByText(/Security and failure alerts are still sent/)).toBeInTheDocument();
  });

  it("the unsubscribe link route highlights the notifications section", async () => {
    render(<MemoryRouter initialEntries={["/settings/notifications"]}><Settings /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Email notifications" });
    expect(section.className).toMatch(/ring-2/);
  });
});

describe("Settings: wallet-change hold", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([] as never);
    vi.mocked(notificationAPI.preferences).mockResolvedValue(PREFS as never);
  });

  it("tells the user when the hold on new withdrawal addresses ends", async () => {
    authUser.wallet_hold_until = new Date(Date.now() + 20 * 3600 * 1000).toISOString();
    authUser.wallet_hold_reason = "password_reset";
    render(<MemoryRouter><Settings /></MemoryRouter>);
    const notice = await screen.findByTestId("wallet-hold");
    expect(notice).toHaveTextContent(/new withdrawal addresses are on hold until .* after a password reset/);
    expect(notice).toHaveTextContent(/addresses you already use keep running/);
    delete authUser.wallet_hold_until; delete authUser.wallet_hold_reason;
  });

  it("shows nothing once the hold has expired", async () => {
    authUser.wallet_hold_until = new Date(Date.now() - 60 * 1000).toISOString();
    render(<MemoryRouter><Settings /></MemoryRouter>);
    await screen.findByText("Exchange API keys");
    expect(screen.queryByTestId("wallet-hold")).toBeNull();
    delete authUser.wallet_hold_until;
  });
});
