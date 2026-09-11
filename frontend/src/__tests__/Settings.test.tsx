import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "@/pages/Settings";

vi.mock("@/api/endpoints", () => ({
  authAPI: { changePassword: vi.fn() },
  userAPI: { exchangeKeys: vi.fn(), addExchangeKey: vi.fn(), verifyExchangeKey: vi.fn(), deleteExchangeKey: vi.fn(), sendVerification: vi.fn(),
             supportedExchanges: vi.fn().mockResolvedValue(["bitget"]) },
  reportsAPI: { performance: vi.fn().mockResolvedValue({ has_data: false }) },
  integrationsAPI: { available: vi.fn().mockResolvedValue([]), webhooks: vi.fn().mockResolvedValue([]) },
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z" }, logout: vi.fn() }) }));
import { userAPI } from "@/api/endpoints";

const FAILED = { id: "k1", exchange: "bitget", api_key_masked: "bg_a...z9", verified: false, verification_status: "failed",
  verification_error: "Bitget rejected this server's IP address (error 40018): Invalid IP. Add the server's IP to the key's allowlist, or create the key without an IP restriction.",
  last_checked_at: "2026-09-11T11:38:00Z", created_at: "2026-09-11T11:37:59Z" };
const VERIFIED_WITHDRAW = { ...FAILED, id: "k2", exchange: "binance", verified: true, verification_status: "verified", verification_error: null,
  verified_at: "2026-09-11T11:40:00Z", withdrawal_enabled: true };

describe("Settings: exchange keys", () => {
  beforeEach(() => { vi.clearAllMocks(); });

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
    expect(screen.getByTestId("server-ip-placeholder")).toHaveTextContent("our server IP will be shown here");
    expect(g).toHaveTextContent(/Bitget address book/);
  });
});
