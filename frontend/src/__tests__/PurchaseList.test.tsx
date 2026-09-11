import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PurchaseList from "@/components/Purchases/PurchaseList";
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

  it("shows withdrawal rows with their chain fee and on-chain tx hash", async () => {
    render(<PurchaseList purchases={[base]} onChanged={vi.fn()} onError={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Transactions" }));
    await screen.findByText("WD-1");
    expect(screen.getByText(/tx 0xabcdef0123456789/)).toBeInTheDocument();
    expect(screen.getByText("0.00003 BTC")).toBeInTheDocument();      // withdrawal fee
    expect(screen.getByText("0.0000015 BTC")).toBeInTheDocument();    // trading fee on the buy
  });
});
