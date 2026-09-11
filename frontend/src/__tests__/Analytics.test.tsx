import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Analytics from "@/components/Dashboard/Analytics";

vi.mock("@/api/endpoints", () => ({
  analyticsAPI: { performance: vi.fn(), recurring: vi.fn(), volatility: vi.fn(), comparison: vi.fn() },
}));
import { analyticsAPI } from "@/api/endpoints";

describe("Analytics", () => {
  it("renders performance, projection, recommendations and the estimates note on every projection", async () => {
    vi.mocked(analyticsAPI.performance).mockResolvedValue({
      price_available: true,
      holdings: [{ crypto: "BTC", total_amount: 0.01, invested_usdt: 500, average_buy_price: 50000, current_price: 60000,
                   market_value: 600, unrealized_pnl_usdt: 100, unrealized_pnl_pct: 20 }],
      total_invested_usdt: 500, total_market_value: 600, total_pnl_usdt: 100, total_pnl_pct: 20,
      best_performer: "BTC", worst_performer: "BTC",
    } as never);
    vi.mocked(analyticsAPI.recurring).mockResolvedValue({ schedules: [], expected_monthly_usdt: 435, projected_annual_usdt: 5220 } as never);
    vi.mocked(analyticsAPI.volatility).mockResolvedValue({
      entries: [{ crypto: "ETH", volatility_30d_pct: 6.1, level: "high", recommendation: "High volatility: smaller, more frequent buys" }] } as never);
    vi.mocked(analyticsAPI.comparison).mockResolvedValue({ entries: [], diversification_score: 46.9 } as never);

    render(<Analytics />);
    await waitFor(() => expect(screen.getByText("100.00 USDT (20%)")).toBeInTheDocument());
    expect(screen.getByText("435.00 USDT")).toBeInTheDocument();
    expect(screen.getByText(/more frequent buys/)).toBeInTheDocument();
    expect(screen.getByText("46.9")).toBeInTheDocument();
    const notes = screen.getAllByTestId("estimates-note");
    expect(notes.length).toBeGreaterThanOrEqual(3);
    for (const n of notes) expect(n).toHaveTextContent("Estimates only, not financial advice; past performance doesn't guarantee future results.");
  });

  it("falls back to cost basis when live prices are unavailable", async () => {
    vi.mocked(analyticsAPI.performance).mockResolvedValue({ price_available: false, holdings: [], total_invested_usdt: 500 } as never);
    vi.mocked(analyticsAPI.recurring).mockResolvedValue({ schedules: [], expected_monthly_usdt: 0, projected_annual_usdt: 0 } as never);
    vi.mocked(analyticsAPI.volatility).mockResolvedValue({ entries: [] } as never);
    vi.mocked(analyticsAPI.comparison).mockResolvedValue({ entries: [], diversification_score: 0 } as never);
    render(<Analytics />);
    await waitFor(() => expect(screen.getByText(/showing cost basis only/)).toBeInTheDocument());
  });
});
