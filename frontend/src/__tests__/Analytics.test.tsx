import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Analytics from "@/components/Dashboard/Analytics";

vi.mock("@/api/endpoints", () => ({
  analyticsAPI: { performance: vi.fn(), recurring: vi.fn(), volatility: vi.fn(), comparison: vi.fn() },
}));
import { analyticsAPI } from "@/api/endpoints";

describe("Analytics", () => {
  it("renders performance, projection, the volatility metric and the estimates note; no ranking, no score, no guidance", async () => {
    vi.mocked(analyticsAPI.performance).mockResolvedValue({
      price_available: true,
      holdings: [{ crypto: "BTC", total_amount: 0.01, invested_usdt: 500, average_buy_price: 50000, price_precision: 2, current_price: 60000,
                   market_value: 600, unrealized_pnl_usdt: 100, unrealized_pnl_pct: 20 },
                 { crypto: "XLM", total_amount: 14.30568, invested_usdt: 3, average_buy_price: 0.20945, price_precision: 5, current_price: 0.21,
                   market_value: 3.0, unrealized_pnl_usdt: 0, unrealized_pnl_pct: 0.3 }],
      total_invested_usdt: 503, total_market_value: 603, total_pnl_usdt: 100, total_pnl_pct: 19.9,
    } as never);
    vi.mocked(analyticsAPI.recurring).mockResolvedValue({ schedules: [], expected_monthly_usdt: 435, projected_annual_usdt: 5220 } as never);
    vi.mocked(analyticsAPI.volatility).mockResolvedValue({
      entries: [{ crypto: "ETH", volatility_30d_pct: 6.1, level: "high", summary: "30-day volatility 6.1% (high)." }] } as never);
    vi.mocked(analyticsAPI.comparison).mockResolvedValue({ entries: [], share_of_invested: [{ crypto: "BTC", share_pct: 75 }, { crypto: "XLM", share_pct: 15 }, { crypto: "XRP", share_pct: 10 }] } as never);

    render(<Analytics />);
    await waitFor(() => expect(screen.getByText("100.00 USDT (19.9%)")).toBeInTheDocument());
    expect(screen.getByText("435.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("30-day volatility 6.1% (high).")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "30-day volatility" })).toBeInTheDocument();
    expect(screen.getByTestId("share-of-invested")).toHaveTextContent("Share of invested value (by cost, not current value): BTC 75% · XLM 15% · XRP 10%");
    expect(screen.queryByText(/Best:|Worst:|Diversification score|cadence|guidance/)).toBeNull();
    // prices follow the pair's precision: BTC 2 decimals, XLM 5
    expect(screen.getByText("50,000.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("0.20945 USDT")).toBeInTheDocument();
    expect(screen.getByText("0.21000 USDT")).toBeInTheDocument();
    const notes = screen.getAllByTestId("estimates-note");
    expect(notes.length).toBeGreaterThanOrEqual(3);
    for (const n of notes) expect(n).toHaveTextContent("Estimates only, not financial advice; past performance doesn't guarantee future results.");
  });

  it("falls back to cost basis when live prices are unavailable", async () => {
    vi.mocked(analyticsAPI.performance).mockResolvedValue({ price_available: false, holdings: [], total_invested_usdt: 500 } as never);
    vi.mocked(analyticsAPI.recurring).mockResolvedValue({ schedules: [], expected_monthly_usdt: 0, projected_annual_usdt: 0 } as never);
    vi.mocked(analyticsAPI.volatility).mockResolvedValue({ entries: [] } as never);
    vi.mocked(analyticsAPI.comparison).mockResolvedValue({ entries: [], share_of_invested: [] } as never);
    render(<Analytics />);
    await waitFor(() => expect(screen.getByText(/showing cost basis only/)).toBeInTheDocument());
    expect(screen.getByText("No holdings or scheduled purchases yet.")).toBeInTheDocument();
  });
});
