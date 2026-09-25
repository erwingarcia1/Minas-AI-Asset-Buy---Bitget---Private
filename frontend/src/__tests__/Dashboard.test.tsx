import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "@/pages/Dashboard";

vi.mock("@/api/endpoints", () => ({
  dashboardAPI: { quickStats: vi.fn(), overview: vi.fn(), timeline: vi.fn(), portfolioSeries: vi.fn() },
  analyticsAPI: { performance: vi.fn(), recurring: vi.fn(), volatility: vi.fn(), comparison: vi.fn() },
}));
import { analyticsAPI, dashboardAPI } from "@/api/endpoints";

const overview = {
  holdings: [{ crypto: "BTC", total_amount: 0.004, on_exchange: 0.004, sent_to_wallet: 0, total_cost_usdt: 300 },
             { crypto: "XRP", total_amount: 1.3320666, on_exchange: 0.0320666, sent_to_wallet: 1.1, total_cost_usdt: 2 }],
  active_purchases: [{ id: "p1", crypto: "BTC", amount_usdt: 100, frequency: "weekly", next_purchase_date: "2026-09-08T00:00:00Z" }],
  upcoming_purchases: [{ id: "p1", crypto: "BTC", amount_usdt: 100, frequency: "weekly", next_purchase_date: "2026-09-08T00:00:00Z" }],
  monthly_average_usdt: 300,
};
const stats = { total_invested_usdt: 300, transaction_count: 3, active_purchases: 1, kyc_status: "verified" };
const series = {
  range: "30D", granularity: "4h", bucket_seconds: 14400,
  points: [{ t: 1_700_000_000, value: 0, invested: 0 }, { t: 1_700_014_400, value: 290, invested: 300 }, { t: 1_700_020_000, value: 310.5, invested: 300 }],
  buys: [{ t: 1_700_010_000, crypto: "BTC", amount: 0.004, cost_usd: 300 }],
  current_value: 310.5, invested: 300, change_usdt: 20.5, change_pct: 7.07, carried_forward: 0, unpriced: [],
  note: "Estimates only, not financial advice.",
};

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardAPI.quickStats).mockResolvedValue(stats as never);
    vi.mocked(dashboardAPI.overview).mockResolvedValue(overview as never);
    vi.mocked(dashboardAPI.portfolioSeries).mockResolvedValue(series as never);
    vi.mocked(analyticsAPI.performance).mockResolvedValue({ price_available: true, holdings: [], total_invested_usdt: 300 } as never);
    vi.mocked(analyticsAPI.recurring).mockResolvedValue({ schedules: [], expected_monthly_usdt: 435, projected_annual_usdt: 5220 } as never);
    vi.mocked(analyticsAPI.volatility).mockResolvedValue({ entries: [] } as never);
    vi.mocked(analyticsAPI.comparison).mockResolvedValue({ entries: [], share_of_invested: [] } as never);
  });

  it("renders stats, holdings, the portfolio chart, upcoming purchases and analytics", async () => {
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText("Holdings")).toHaveTextContent("BTC"));
    expect(screen.getByTestId("holding-BTC")).toHaveTextContent("0.004");
    expect(screen.getByTestId("holding-BTC")).toHaveTextContent("all on the exchange");
    expect(screen.getByTestId("holding-XRP")).toHaveTextContent("1.3320666");
    expect(screen.getByTestId("holding-XRP")).toHaveTextContent("0.0320666 on exchange · 1.1 in your wallet");
    expect(screen.getAllByText("300.00 USDT").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Upcoming purchases")).toHaveTextContent("Weekly");
    expect(dashboardAPI.timeline).not.toHaveBeenCalled();
    await waitFor(() => expect(dashboardAPI.portfolioSeries).toHaveBeenCalledWith("30D"));   // the chart's effect runs after the holdings render
    await waitFor(() => expect(screen.getByTestId("portfolio-current")).toHaveTextContent("310.50 USDT"));
    expect(screen.getByTestId("portfolio-change")).toHaveTextContent("+20.50 USDT (+7.07%) · 30D");
    expect(screen.getByTestId("portfolio-chart")).toBeInTheDocument();
    expect(screen.getByLabelText("Portfolio value")).toHaveTextContent("Estimates only, not financial advice");
    await waitFor(() => expect(screen.getByLabelText("Analytics")).toHaveTextContent("435.00 USDT"));
    expect(screen.queryByText(/Identity not verified/)).not.toBeInTheDocument();
  });

  it("keeps holdings, performance and the donut when the series endpoint fails (503)", async () => {
    const err = Object.assign(new Error("Bitget market data is temporarily unavailable. Please try again in a minute."), { response: { status: 503 } });
    vi.mocked(dashboardAPI.portfolioSeries).mockRejectedValue(err);
    vi.mocked(analyticsAPI.performance).mockResolvedValue({
      price_available: true, total_invested_usdt: 300, total_market_value: 310.5,
      holdings: [{ crypto: "BTC", total_amount: 0.004, invested_usdt: 300, average_buy_price: 75000, current_price: 77625, market_value: 310.5 }],
    } as never);
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId("portfolio-unavailable")).toHaveTextContent("Portfolio chart unavailable right now"));
    expect(screen.getByTestId("portfolio-unavailable")).toHaveTextContent("Holdings and performance figures are unaffected");
    expect(screen.queryByTestId("portfolio-chart")).not.toBeInTheDocument();
    expect(screen.getByTestId("holding-BTC")).toHaveTextContent("0.004");
    await waitFor(() => expect(screen.getByTestId("donut-total")).toHaveTextContent("310.50 USDT"));
    expect(screen.getByLabelText("Analytics")).toHaveTextContent("Market value");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows retry on failure and recovers", async () => {
    vi.mocked(dashboardAPI.overview).mockRejectedValueOnce(new Error("boom"));
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("boom"));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByLabelText("Holdings")).toHaveTextContent("BTC"));
  });

  it("nudges unverified users to KYC", async () => {
    vi.mocked(dashboardAPI.quickStats).mockResolvedValue({ ...stats, kyc_status: "pending" } as never);
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/Identity not verified/)).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Verify now" })).toHaveAttribute("href", "/kyc");
  });
});
