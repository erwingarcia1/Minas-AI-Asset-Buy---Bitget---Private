import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Analytics from "../components/Dashboard/Analytics";

vi.mock("../services/api", () => ({
  analytics: { performance: vi.fn(), recurring: vi.fn(), volatility: vi.fn(), comparison: vi.fn() },
}));
import { analytics } from "../services/api";

describe("Analytics", () => {
  it("renders performance, projection, and recommendations", async () => {
    vi.mocked(analytics.performance).mockResolvedValue({
      price_available: true,
      holdings: [{ crypto: "BTC", total_amount: 0.01, invested_usdt: 500,
                   average_buy_price: 50000, current_price: 60000,
                   market_value: 600, unrealized_pnl_usdt: 100, unrealized_pnl_pct: 20 }],
      total_invested_usdt: 500, total_market_value: 600,
      total_pnl_usdt: 100, total_pnl_pct: 20,
      best_performer: "BTC", worst_performer: "BTC",
    } as never);
    vi.mocked(analytics.recurring).mockResolvedValue({
      schedules: [], expected_monthly_usdt: 435, projected_annual_usdt: 5220 } as never);
    vi.mocked(analytics.volatility).mockResolvedValue({
      entries: [{ crypto: "ETH", volatility_30d_pct: 6.1, level: "high",
                  recommendation: "High volatility: smaller, more frequent buys" }] } as never);
    vi.mocked(analytics.comparison).mockResolvedValue(
      { entries: [], diversification_score: 46.9 } as never);

    render(<Analytics />);
    await waitFor(() => expect(screen.getByText("$100.00 (20%)")).toBeInTheDocument());
    expect(screen.getByText("$435.00")).toBeInTheDocument();
    expect(screen.getByText(/more frequent buys/)).toBeInTheDocument();
    expect(screen.getByText("46.9")).toBeInTheDocument();
  });
});
