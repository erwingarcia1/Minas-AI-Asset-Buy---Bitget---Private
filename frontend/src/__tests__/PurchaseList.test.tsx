import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PurchaseList from "@/components/Purchases/PurchaseList";
import { useAuthStore } from "@/store/authStore";
import type { AutoBuyPurchase, AutoBuyTransaction } from "@/types";

vi.mock("@/api/endpoints", () => ({
  autoBuyAPI: { update: vi.fn(), remove: vi.fn(), transactions: vi.fn(), retryWithdrawal: vi.fn() },
  marketAPI: { coin: vi.fn() },
}));
import { autoBuyAPI } from "@/api/endpoints";

const base: AutoBuyPurchase = {
  id: "p1", crypto_symbol: "BTC", amount_usd: 60, frequency: "weekly", wallet_address: "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
  blockchain_network: "BTC", purchase_type: "autobuy", withdraw_immediately: true, exchange_account_id: "k1", exchange: "bitget",
  status: "withdrawal_failed", failure_count: 0, last_error: "Bitget has BTC withdrawals on BTC suspended",
  next_execution: null, created_at: "2026-09-11T10:00:00Z", updated_at: "2026-09-11T10:00:00Z", latest_transaction: null,
};

const buy: AutoBuyTransaction = { id: "t1", purchase_id: "p1", exchange_tx_id: "ORD-1", tx_type: "buy", status: "success",
  amount_executed: 0.00077, fee: 0.0000015, fee_currency: "BTC", error_message: null, executed_at: "2026-09-11T10:05:00Z", created_at: "2026-09-11T10:05:00Z" };
const wd: AutoBuyTransaction = { id: "t2", purchase_id: "p1", exchange_tx_id: "WD-1", tx_type: "withdrawal", status: "success",
  amount_executed: 0.00077, fee: null, error_message: null, executed_at: "2026-09-11T10:06:00Z", created_at: "2026-09-11T10:06:00Z",
  parent_transaction_id: "t1", withdrawal_id: "WD-1", withdrawal_chain: "BTC", withdrawal_fee: 0.00003, withdrawal_fee_currency: "BTC",
  tx_hash: "0xabcdef0123456789abcdef", raw_status: "success" };

describe("PurchaseList: parked purchases and withdrawal rows", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", withdrawals_allowed: true } } as never);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(autoBuyAPI.transactions).mockResolvedValue([wd, buy] as never);
    vi.mocked(autoBuyAPI.retryWithdrawal).mockResolvedValue({ purchase_id: "p1", status: "active", next_execution: "2026-09-18T10:00:00Z", withdrawal: wd } as never);
  });

  it("explains a failed withdrawal and offers to retry only the withdrawal", async () => {
    const onChanged = vi.fn();
    render(<PurchaseList purchases={[base]} onChanged={onChanged} onError={vi.fn()} />);
    expect(screen.getByTestId("parked-help")).toHaveTextContent(/coins are still on the exchange.*the buy is never repeated/);
    fireEvent.click(screen.getByRole("button", { name: "Retry withdrawal" }));
    await waitFor(() => expect(autoBuyAPI.retryWithdrawal).toHaveBeenCalledWith("p1"));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/No new buy is placed/));
    expect(autoBuyAPI.update).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalled();
  });

  it("does not offer the retry on active purchases, and needs_info says what is missing", () => {
    render(<PurchaseList purchases={[{ ...base, status: "active" }, { ...base, id: "p2", status: "needs_info" }]} onChanged={vi.fn()} onError={vi.fn()} />);
    expect(screen.getAllByRole("button", { name: "Retry withdrawal" })).toHaveLength(1);
    expect(screen.getByText(/beneficiary \(travel-rule\) details/)).toBeInTheDocument();
  });

  it("accumulate purchases show where coins are held instead of a network", () => {
    render(<PurchaseList purchases={[{ ...base, id: "d1", purchase_type: "dca", withdraw_immediately: false, status: "active", blockchain_network: null, wallet_address: null, last_error: null }]} onChanged={vi.fn()} onError={vi.fn()} />);
    expect(screen.getByTestId("purchase-venue")).toHaveTextContent("Bitget · Held on Bitget");
    expect(screen.getByTestId("purchase-venue").textContent).not.toMatch(/BTC|ERC20/);
  });

  it("private beta: a paused withdraw-mode purchase offers Switch to accumulate, not Resume, to users who are not allowed", () => {
    useAuthStore.setState({ user: { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", withdrawals_allowed: false } } as never);
    render(<PurchaseList purchases={[{ ...base, status: "paused", last_error: null },
      { ...base, id: "d1", purchase_type: "dca", withdraw_immediately: false, status: "paused", blockchain_network: null, wallet_address: null, last_error: null }]} onChanged={vi.fn()} onError={vi.fn()} />);
    expect(screen.getAllByRole("button", { name: "Resume" })).toHaveLength(1);   // only the accumulate purchase
    expect(screen.getByRole("button", { name: "Switch to accumulate" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch to withdraw" })).toBeNull();
  });

  it("shows the low-balance banner only while a warning is outstanding for the next run", () => {
    render(<PurchaseList purchases={[{ ...base, id: "a1", status: "active", last_error: null, next_execution: "2026-10-05T00:12:08Z", low_balance_for_next_run: true, low_balance_alerted_at: "2026-10-04T00:15:00Z" },
      { ...base, id: "a2", status: "active", last_error: null, next_execution: "2026-10-05T00:12:08Z", low_balance_for_next_run: false }]} onChanged={vi.fn()} onError={vi.fn()} />);
    const banners = screen.getAllByTestId("low-balance-banner");
    expect(banners).toHaveLength(1);
    expect(banners[0]).toHaveTextContent(/^Balance low for your next buy on .+\.$/);
  });

  it("an allowed user still gets Resume on a paused withdraw-mode purchase", () => {
    render(<PurchaseList purchases={[{ ...base, status: "paused", last_error: null }]} onChanged={vi.fn()} onError={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled();
  });

  it("says when the exchange key was removed and does not offer to resume", () => {
    render(<PurchaseList purchases={[{ ...base, status: "paused", exchange: null, exchange_account_id: null, exchange_key_removed: true,
      last_error: "Exchange key removed. Add an exchange key and create a new purchase." }]} onChanged={vi.fn()} onError={vi.fn()} />);
    expect(screen.getByTestId("purchase-venue")).toHaveTextContent("Exchange key removed");
    expect(screen.getByTestId("purchase-venue").textContent).not.toContain("—");
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Retry withdrawal" })).toBeNull();
  });

  it("shows a simulated (dry-run) withdrawal as simulated with trimmed amounts", async () => {
    vi.mocked(autoBuyAPI.transactions).mockResolvedValue([{ ...wd, status: "simulated", tx_hash: null, raw_status: "simulated",
      amount_executed: 0.00073179, expected_received: 0.00073179, withdrawal_fee: 0.00003000 }, { ...buy, amount_executed: 0.00076332, fee: 0.00000153 }] as never);
    render(<PurchaseList purchases={[{ ...base, status: "completed" }]} onChanged={vi.fn()} onError={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Transactions" }));
    await screen.findByText("WD-1");
    expect(screen.getByText("simulated")).toBeInTheDocument();
    expect(screen.getByText("dry run: nothing was sent")).toBeInTheDocument();
    expect(screen.getByText("0.00073179 BTC")).toBeInTheDocument();
    expect(screen.getByText("0.00003 BTC")).toBeInTheDocument();
    expect(screen.getByText("0.00000153 BTC")).toBeInTheDocument();
    expect(screen.queryByText(/0\.00003000/)).toBeNull();
  });

  it("shows withdrawal rows with their chain fee and on-chain tx hash", async () => {
    render(<PurchaseList purchases={[base]} onChanged={vi.fn()} onError={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Transactions" }));
    await screen.findByText("WD-1");
    expect(screen.getByText(/tx 0xabcdef0123456789/)).toBeInTheDocument();
    expect(screen.getByText("0.00003 BTC")).toBeInTheDocument();      // withdrawal fee
    expect(screen.getByText("0.0000015 BTC")).toBeInTheDocument();    // trading fee on the buy
  });
});

describe("PurchaseList: private beta, withdrawals not allowed", () => {
  it("offers neither 'Switch to withdraw' nor 'Retry withdrawal', but keeps pause/delete", () => {
    useAuthStore.setState({ user: { id: "u2", email: "tester@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", withdrawals_allowed: false } } as never);
    render(<PurchaseList purchases={[{ ...base, status: "active", purchase_type: "dca" }, { ...base, id: "p2" }]} onChanged={vi.fn()} onError={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Switch to withdraw" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry withdrawal" })).toBeNull();
    expect(screen.getAllByRole("button", { name: "Delete" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });
});
