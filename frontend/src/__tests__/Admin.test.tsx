import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Admin from "@/pages/Admin";

vi.mock("@/api/endpoints", () => ({
  adminAPI: { stats: vi.fn(), users: vi.fn(), suspend: vi.fn(), unsuspend: vi.fn(), removeUser: vi.fn(), transactions: vi.fn(), alerts: vi.fn(), liftWalletHold: vi.fn(),
              purchases: vi.fn(), reviewPurchase: vi.fn(), resolvePurchase: vi.fn() },
}));
const authUser = { current: { id: "a1", email: "admin@example.com", kyc_status: "verified", role: "admin", created_at: "" } as Record<string, unknown> };
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: authUser.current }) }));
import { adminAPI } from "@/api/endpoints";

const STATS = { total_users: 12, verified_users: 9, suspended_users: 1, total_invested_usdt: 1234.5, total_transactions: 40,
  failed_transactions_24h: 2, active_purchases: 7, signups_7d: 3, volume_7d_usdt: 250 };
const ROW = { id: "u1", email: "maria@example.com", role: "user", kyc_status: "verified", suspended: false, created_at: "2026-09-01T00:00:00Z", invested_usdt: 300 };

function mount() {
  return render(
    <MemoryRouter initialEntries={["/admin"]}>
      <Routes><Route path="/admin" element={<Admin />} /><Route path="/dashboard" element={<p>dashboard page</p>} /></Routes>
    </MemoryRouter>);
}

describe("Admin console", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authUser.current = { id: "a1", email: "admin@example.com", kyc_status: "verified", role: "admin", created_at: "" };
    vi.mocked(adminAPI.stats).mockResolvedValue(STATS as never);
    vi.mocked(adminAPI.users).mockResolvedValue({ items: [ROW], total: 1 } as never);
    vi.mocked(adminAPI.suspend).mockResolvedValue(undefined as never);
    vi.mocked(adminAPI.removeUser).mockResolvedValue(undefined as never);
  });

  it("redirects non-admins away without calling any admin endpoint (the server enforces the role anyway)", async () => {
    authUser.current = { ...authUser.current, role: "user" };
    mount();
    await screen.findByText("dashboard page");
    expect(adminAPI.stats).not.toHaveBeenCalled();
  });

  it("shows platform stats and the user list", async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId("admin-overview")).toHaveTextContent("12"));
    fireEvent.click(screen.getByRole("button", { name: "Users" }));
    await waitFor(() => expect(screen.getByTestId("admin-users")).toHaveTextContent("maria@example.com"));
  });

  it("suspending requires typing the user's email; cancel does nothing", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Users" }));
    fireEvent.click(await screen.findByRole("button", { name: "Suspend" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Suspend this user?");
    const confirmBtn = screen.getByRole("button", { name: "Suspend user" });
    expect(confirmBtn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "wrong@example.com" } });
    expect(confirmBtn).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(adminAPI.suspend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Suspend" }));
    fireEvent.change(await screen.findByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Suspend user" }));
    await waitFor(() => expect(adminAPI.suspend).toHaveBeenCalledWith("u1"));
  });

  it("deleting sends the typed email as the confirmation and surfaces a server rejection", async () => {
    vi.mocked(adminAPI.removeUser).mockRejectedValueOnce({ isAxiosError: true, response: { status: 400, data: { detail: "confirm must equal the user's email" } } });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Users" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.change(await screen.findByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete user" }));
    await waitFor(() => expect(adminAPI.removeUser).toHaveBeenCalledWith("u1", "maria@example.com"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/confirm must equal/));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("Admin: wallet-change hold override", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authUser.current = { id: "a1", email: "admin@example.com", kyc_status: "verified", role: "admin", created_at: "" };
    vi.mocked(adminAPI.stats).mockResolvedValue(STATS as never);
  });

  async function openUsers() {
    mount();
    await waitFor(() => expect(screen.getByTestId("admin-overview")).toHaveTextContent("12"));
    fireEvent.click(screen.getByRole("button", { name: "Users" }));
  }

  it("shows the hold, requires a reason and the typed email, then calls the override endpoint", async () => {
    const until = new Date(Date.now() + 10 * 3600 * 1000).toISOString();
    vi.mocked(adminAPI.users).mockResolvedValue({ items: [{ ...ROW, wallet_hold_until: until, wallet_hold_reason: "password_reset" }], total: 1 } as never);
    vi.mocked(adminAPI.liftWalletHold).mockResolvedValue({} as never);
    await openUsers();
    expect(await screen.findByTestId("hold-until")).toHaveTextContent(/Wallet hold until/);
    fireEvent.click(screen.getByRole("button", { name: "Lift wallet hold" }));
    const confirmBtn = screen.getByRole("button", { name: "Lift hold" });
    fireEvent.change(screen.getByLabelText(/Type .* to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(confirmBtn);   // no reason yet: refused locally, the API is not called
    expect(await screen.findByText(/how the customer proved their identity/)).toBeInTheDocument();
    expect(adminAPI.liftWalletHold).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/How did the customer prove their identity/), { target: { value: "video call with ID, ticket #4711" } });
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(adminAPI.liftWalletHold).toHaveBeenCalledWith("u1", "maria@example.com", "video call with ID, ticket #4711"));
    expect(await screen.findByText(/Wallet-change hold lifted for maria@example.com/)).toBeInTheDocument();
  });

  it("offers no override when there is no active hold", async () => {
    vi.mocked(adminAPI.users).mockResolvedValue({ items: [{ ...ROW, wallet_hold_until: null }], total: 1 } as never);
    await openUsers();
    await screen.findByText("maria@example.com");
    expect(screen.queryByRole("button", { name: "Lift wallet hold" })).toBeNull();
  });
});

describe("Admin: review queue for parked purchases", () => {
  const PARKED = { id: "pp1", user_id: "u1", email: "maria@example.com", crypto_symbol: "BTC", amount_usd: 50, frequency: "weekly", purchase_type: "dca",
    status: "needs_review", failure_count: 0, last_error: "order ORD-9: fill reports amount 0", next_execution: null, updated_at: "2026-09-14T10:00:00Z",
    parked_attempts: [{ id: "t1", client_oid: "ab-1", order_id: "ORD-9", status: "needs_review", error_message: "fill reports amount 0", created_at: "2026-09-14T10:00:00Z", executed_at: null }] };
  beforeEach(() => {
    vi.clearAllMocks();
    authUser.current = { id: "a1", email: "admin@example.com", kyc_status: "verified", role: "admin", created_at: "" };
    vi.mocked(adminAPI.stats).mockResolvedValue(STATS as never);
    vi.mocked(adminAPI.purchases).mockResolvedValue({ items: [PARKED], total: 1 } as never);
  });

  it("checks the exchange, shows the verdict, requires a note, then resolves", async () => {
    vi.mocked(adminAPI.reviewPurchase).mockResolvedValue({ purchase: PARKED, actions: ["record_fill", "mark_not_sent", "cancel"],
      lookups: [{ attempt_id: "t1", client_oid: "ab-1", state: "filled", fill: { amount_crypto: 0.001, cost_usdt: 50, exchange_rate: 50000, raw_status: "filled" }, verdict: "ok", error: null }] } as never);
    vi.mocked(adminAPI.resolvePurchase).mockResolvedValue({ ...PARKED, status: "active" } as never);
    mount();
    await waitFor(() => expect(screen.getByTestId("admin-overview")).toHaveTextContent("12"));
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(await screen.findByTestId("review-row")).toHaveTextContent(/fill reports amount 0/);
    fireEvent.click(screen.getByRole("button", { name: "Check exchange" }));
    expect(await screen.findByTestId("verdict")).toHaveTextContent("fill passes the guard");
    fireEvent.click(screen.getByRole("button", { name: "Record fill" }));
    expect(await screen.findByText(/Write what you checked/)).toBeInTheDocument();
    expect(adminAPI.resolvePurchase).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/What did you check/), { target: { value: "Bitget order history shows ORD-9 filled" } });
    fireEvent.click(screen.getByRole("button", { name: "Record fill" }));
    await waitFor(() => expect(adminAPI.resolvePurchase).toHaveBeenCalledWith("pp1", "record_fill", "Bitget order history shows ORD-9 filled"));
    expect(await screen.findByText(/resolved: now active/)).toBeInTheDocument();
  });
});

describe("Admin overview: signups per day", () => {
  it("draws one bar per day from signups_by_day with an accessible count", async () => {
    vi.mocked(adminAPI.stats).mockResolvedValue({ ...STATS, signups_by_day: [
      { day: "2026-09-27", count: 0 }, { day: "2026-09-28", count: 3 }] } as never);
    render(<MemoryRouter initialEntries={["/admin"]}><Routes><Route path="/admin" element={<Admin />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId("signups-by-day")).toBeInTheDocument());
    const strip = screen.getByTestId("signups-by-day");
    expect(strip).toHaveTextContent("Signups per day");
    expect(strip.querySelectorAll("li")).toHaveLength(2);
    expect(strip).toHaveTextContent("2026-09-28: 3 signups");
    expect(strip).toHaveTextContent("leaked invite code");
  });
});

describe("Admin: review queue, keep coins on Bitget", () => {
  it("offers 'Record fill, keep on Bitget' only for withdraw-mode purchases and sends keep_on_exchange", async () => {
    const parked = { id: "pp1", user_id: "u1", email: "maria@example.com", crypto_symbol: "BTC", amount_usd: 50, frequency: "weekly", purchase_type: "autobuy",
      status: "needs_review", failure_count: 0, last_error: "order ORD-9: fill reports amount 0", next_execution: null, updated_at: "2026-09-14T10:00:00Z",
      parked_attempts: [{ id: "t1", client_oid: "ab-1", order_id: "ORD-9", status: "needs_review", error_message: "fill reports amount 0", created_at: "2026-09-14T10:00:00Z", executed_at: null }] };
    vi.mocked(adminAPI.stats).mockResolvedValue(STATS as never);
    vi.mocked(adminAPI.purchases).mockResolvedValue({ items: [parked], total: 1 } as never);
    vi.mocked(adminAPI.reviewPurchase).mockResolvedValue({ purchase: parked, actions: ["record_fill", "mark_not_sent", "cancel"],
      lookups: [{ attempt_id: "t1", client_oid: "ab-1", state: "filled", fill: { amount_crypto: 0.001, cost_usdt: 50, exchange_rate: 50000, raw_status: "filled" }, verdict: "ok", error: null }] } as never);
    vi.mocked(adminAPI.resolvePurchase).mockResolvedValue({ ...parked, status: "active", purchase_type: "dca" } as never);
    render(<MemoryRouter initialEntries={["/admin"]}><Routes><Route path="/admin" element={<Admin />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId("admin-overview")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    await screen.findByTestId("review-row");
    fireEvent.click(screen.getByRole("button", { name: "Check exchange" }));
    const keep = await screen.findByRole("button", { name: "Record fill, keep on Bitget" });
    expect(keep).toHaveAttribute("title", expect.stringMatching(/no withdrawal.*switches to accumulate/));
    fireEvent.change(screen.getByLabelText(/What did you check/), { target: { value: "Bitget order history shows ORD-9 filled" } });
    fireEvent.click(keep);
    await waitFor(() => expect(adminAPI.resolvePurchase).toHaveBeenCalledWith("pp1", "record_fill", "Bitget order history shows ORD-9 filled", undefined, true));
    expect(await screen.findByText(/resolved: now active/)).toBeInTheDocument();
  });
});
