import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "@/pages/Dashboard";

vi.mock("@/api/endpoints", () => ({
  dashboardAPI: { quickStats: vi.fn(), overview: vi.fn(), timeline: vi.fn() },
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
const timeline = { days: 30, timeline: [{ date: "2026-08-31", transaction_count: 3, total_usdt: 300 }] };

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardAPI.quickStats).mockResolvedValue(stats as never);
    vi.mocked(dashboardAPI.overview).mockResolvedValue(overview as never);
    vi.mocked(dashboardAPI.timeline).mockResolvedValue(timeline as never);
    vi.mocked(analyticsAPI.performance).mockResolvedValue({ price_available: true, holdings: [], total_invested_usdt: 300 } as never);
    vi.mocked(analyticsAPI.recurring).mockResolvedValue({ schedules: [], expected_monthly_usdt: 435, projected_annual_usdt: 5220 } as never);
    vi.mocked(analyticsAPI.volatility).mockResolvedValue({ entries: [] } as never);
    vi.mocked(analyticsAPI.comparison).mockResolvedValue({ entries: [], share_of_invested: [] } as never);
  });

  it("renders stats, holdings, the 30-day timeline, upcoming purchases and analytics", async () => {
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText("Holdings")).toHaveTextContent("BTC"));
    expect(screen.getByTestId("holding-BTC")).toHaveTextContent("0.004");
    expect(screen.getByTestId("holding-BTC")).toHaveTextContent("all on the exchange");
    expect(screen.getByTestId("holding-XRP")).toHaveTextContent("1.3320666");
    expect(screen.getByTestId("holding-XRP")).toHaveTextContent("0.0320666 on exchange · 1.1 in your wallet");
    expect(screen.getAllByText("300.00 USDT").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Upcoming purchases")).toHaveTextContent("Weekly");
    expect(dashboardAPI.timeline).toHaveBeenCalledWith(30);
    expect(screen.getByLabelText("Investment timeline")).toHaveTextContent("last 30 days");
    expect(screen.getByTestId("timeline-total")).toHaveTextContent("300.00 USDT");
    expect(screen.getByTestId("timeline-chart")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Analytics")).toHaveTextContent("435.00 USDT"));
    expect(screen.queryByText(/Identity not verified/)).not.toBeInTheDocument();
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
