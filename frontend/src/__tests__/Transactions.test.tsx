import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Transactions from "@/pages/Transactions";

vi.mock("@/api/endpoints", () => ({ transactionAPI: { list: vi.fn(), stats: vi.fn() } }));
import { transactionAPI } from "@/api/endpoints";

const ok = { id: "t1", purchase_id: "p1", crypto: "BTC", purchase_type: "dca", tx_type: "buy", status: "completed", amount_crypto: 0.00019134,
  cost_usdt: 15, exchange_rate: 78395.01, fee: null, fee_currency: null, exchange: "bitget", order_id: "DRY-ab-1", withdrawal_id: null,
  wallet_address: null, blockchain_network: null, withdrawal_fee: null, withdrawal_fee_currency: null, tx_hash: null, error_message: null,
  executed_at: "2026-09-14T15:46:58Z", created_at: "2026-09-14T15:46:58Z" };
const failed = { ...ok, id: "t2", status: "failed", amount_crypto: 0, cost_usdt: 0, exchange_rate: null, order_id: "DRY-ab-2",
  error_message: "NETWORK: Bitget error NETWORK: name resolution", executed_at: "2026-09-14T14:39:51Z", created_at: "2026-09-14T14:39:51Z" };

describe("Transactions page: failed attempts are distinct and the tiles agree", () => {
  beforeEach(() => {
    vi.mocked(transactionAPI.list).mockResolvedValue({ items: [ok, failed], total: 2 } as never);
    vi.mocked(transactionAPI.stats).mockResolvedValue({ total_transactions: 16, total_invested_usdt: 255, average_purchase_usdt: 31.875,
      completed_count: 8, failed_count: 6, pending_count: 0, needs_review_count: 1 } as never);
  });

  it("tiles are labelled by what they count: completed buys, invested over completed buys, average per completed buy", async () => {
    render(<MemoryRouter><Transactions /></MemoryRouter>);
    expect(await screen.findByTestId("tile-completed")).toHaveTextContent(/Completed buys\s*8/);
    expect(screen.getByTestId("tile-completed")).toHaveTextContent(/6 failed attempts · 1 under review · nothing spent on those/);
    expect(screen.getByTestId("tile-invested")).toHaveTextContent(/255\.00 USDT/);
    expect(screen.getByTestId("tile-invested")).toHaveTextContent(/completed buys only/);
    expect(screen.getByTestId("tile-average")).toHaveTextContent(/Average per completed buy/);
    expect(screen.queryByText("Total transactions")).toBeNull();
  });

  it("a failed attempt shows no cost, no amount, no rate and says it was not bought", async () => {
    render(<MemoryRouter><Transactions /></MemoryRouter>);
    const row = await screen.findByTestId("failed-row");
    expect(row).toHaveTextContent(/Attempt failed — not bought/);
    expect(row).toHaveTextContent(/nothing spent/);
    expect(row.textContent).not.toMatch(/15\.00 USDT/);
    expect(row).toHaveTextContent(/failed/);
    expect(row).toHaveTextContent(/name resolution/);
  });
});
