import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "../components/Dashboard/Dashboard";

vi.mock("../services/api", () => ({
  user: { getProfile: vi.fn() },
  dashboard: { get: vi.fn(), getStats: vi.fn(), getTimeline: vi.fn() },
  transactions: { list: vi.fn() },
}));
import { dashboard, transactions, user } from "../services/api";

const profile = { id: "u1", email: "maria@example.com", kyc_status: "verified",
                  created_at: "2026-09-01T00:00:00Z" };
const dash = {
  holdings: [{ crypto: "BTC", total_amount: 0.004, total_cost_usdt: 300 }],
  active_purchases: [{ id: "p1", crypto: "BTC", amount_usdt: 100, frequency: "weekly",
                       next_purchase_date: "2026-09-08T00:00:00Z", enabled: true,
                       created_at: "", updated_at: "" }],
  upcoming_purchases: [{ id: "p1", crypto: "BTC", amount_usdt: 100, frequency: "weekly",
                         next_purchase_date: "2026-09-08T00:00:00Z", enabled: true,
                         created_at: "", updated_at: "" }],
  monthly_average_usdt: 300,
};
const stats = { total_invested_usdt: 300, transaction_count: 3,
                active_purchases: 1, kyc_status: "verified" };
const timeline = { days: 30, timeline: [{ date: "2026-08-31", transaction_count: 3, total_usdt: 300 }] };
const txs = { items: [{ id: "t1", crypto: "BTC", amount_crypto: 0.001, cost_usdt: 100,
                        status: "completed", binance_order_id: "123", created_at: "2026-08-31T10:00:00Z",
                        updated_at: "" }], total: 1, limit: 10, offset: 0 };

describe("Dashboard", () => {
  beforeEach(() => {
    vi.mocked(user.getProfile).mockResolvedValue(profile as never);
    vi.mocked(dashboard.get).mockResolvedValue(dash as never);
    vi.mocked(dashboard.getStats).mockResolvedValue(stats as never);
    vi.mocked(dashboard.getTimeline).mockResolvedValue(timeline as never);
    vi.mocked(transactions.list).mockResolvedValue(txs as never);
  });

  it("renders holdings, stats, upcoming and recent transactions", async () => {
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("maria@example.com")).toBeInTheDocument());
    expect(screen.getByLabelText("Holdings")).toHaveTextContent("BTC");
    expect(screen.getAllByText("$300.00").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Upcoming purchases")).toHaveTextContent("weekly");
    expect(screen.getByLabelText("Recent transactions")).toHaveTextContent("completed");
  });

  it("shows retry on failure and recovers", async () => {
    vi.mocked(user.getProfile).mockRejectedValueOnce(new Error("boom"));
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load"));
    screen.getByRole("button", { name: "Retry" }).click();
    await waitFor(() => expect(screen.getByText("maria@example.com")).toBeInTheDocument());
  });
});
