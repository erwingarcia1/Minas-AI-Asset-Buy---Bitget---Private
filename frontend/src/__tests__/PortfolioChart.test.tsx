import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PortfolioChart, { buysByBucket, changeText } from "@/components/Dashboard/PortfolioChart";
import { REFRESH_INTERVAL_MS, notifyPortfolioChanged } from "@/hooks/usePortfolioRefresh";
import type { PortfolioSeries } from "@/types";

vi.mock("@/api/endpoints", () => ({ dashboardAPI: { portfolioSeries: vi.fn() } }));
import { dashboardAPI } from "@/api/endpoints";

const series: PortfolioSeries = {
  range: "30D", granularity: "4h", bucket_seconds: 14400,
  points: [{ t: 1_700_000_000, value: 0, invested: 0 }, { t: 1_700_014_400, value: 25.1, invested: 26 }, { t: 1_700_020_000, value: 26.2, invested: 26 }],
  buys: [{ t: 1_700_010_000, crypto: "BTC", amount: 0.000196803, cost_usd: 15 }, { t: 1_700_010_100, crypto: "XLM", amount: 33.28668, cost_usd: 11 }],
  current_value: 26.2, invested: 26, change_usdt: 1.1, change_pct: 4.38, carried_forward: 2, unpriced: [], note: "Estimates only, not financial advice.",
};

describe("PortfolioChart helpers", () => {
  it("formats the change with sign and range, or a dash", () => {
    expect(changeText(series)).toBe("+1.10 USDT (+4.38%)");
    expect(changeText({ ...series, change_usdt: -0.5, change_pct: -1.9 })).toBe("−0.50 USDT (−1.9%)");
    expect(changeText({ ...series, change_usdt: 3, change_pct: null })).toBe("+3.00 USDT");
    expect(changeText({ ...series, change_usdt: null })).toBe("—");
  });
  it("groups buys onto the bucket they landed in", () => {
    const m = buysByBucket(series);
    expect([...m.keys()]).toEqual([1_700_014_400]);
    expect(m.get(1_700_014_400)?.map((b) => b.crypto)).toEqual(["BTC", "XLM"]);
  });
});

describe("PortfolioChart", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(dashboardAPI.portfolioSeries).mockResolvedValue(series); });
  afterEach(() => { vi.useRealTimers(); });

  it("renders the headline, the chart, buy count, carried-forward note and the estimates note", async () => {
    render(<PortfolioChart />);
    await waitFor(() => expect(screen.getByTestId("portfolio-current")).toHaveTextContent("26.20 USDT"));
    expect(screen.getByTestId("portfolio-change")).toHaveTextContent("+1.10 USDT (+4.38%) · 30D");
    expect(screen.getByTestId("portfolio-chart")).toBeInTheDocument();
    expect(screen.getByTestId("portfolio-summary")).toHaveTextContent("2 buys in range");
    expect(screen.getByText(/2 buckets priced at the previous close/)).toBeInTheDocument();
    expect(screen.getByTestId("estimates-note")).toHaveTextContent("Estimates only, not financial advice");
    expect(screen.getByRole("button", { name: "30D" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switches ranges with keyboard-reachable buttons", async () => {
    render(<PortfolioChart />);
    await waitFor(() => expect(dashboardAPI.portfolioSeries).toHaveBeenCalledWith("30D"));
    fireEvent.click(screen.getByRole("button", { name: "7D" }));
    await waitFor(() => expect(dashboardAPI.portfolioSeries).toHaveBeenLastCalledWith("7D"));
    expect(screen.getByRole("button", { name: "7D" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "30D" })).toHaveAttribute("aria-pressed", "false");
  });

  it("shows the empty and short-history states", async () => {
    vi.mocked(dashboardAPI.portfolioSeries).mockResolvedValue({ ...series, points: [], buys: [], current_value: null, change_usdt: null, invested: 0 });
    const { unmount } = render(<PortfolioChart />);
    await waitFor(() => expect(screen.getByText("No completed purchases yet.")).toBeInTheDocument());
    unmount();
    vi.mocked(dashboardAPI.portfolioSeries).mockResolvedValue({ ...series, points: [series.points[2]] });
    render(<PortfolioChart />);
    await waitFor(() => expect(screen.getByText("Not enough history for this range yet.")).toBeInTheDocument());
  });

  it("shows an unavailable state with retry when the endpoint fails, without an error alert", async () => {
    vi.mocked(dashboardAPI.portfolioSeries).mockRejectedValueOnce(new Error("Bitget market data is temporarily unavailable. Please try again in a minute."));
    render(<PortfolioChart />);
    await waitFor(() => expect(screen.getByTestId("portfolio-unavailable")).toHaveTextContent("temporarily unavailable"));
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("portfolio-current")).toHaveTextContent("26.20 USDT"));
  });

  it("refetches silently on visibility regain, after a purchase action, and every 5 minutes while visible; never polls faster", async () => {
    vi.useFakeTimers();
    render(<PortfolioChart />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(dashboardAPI.portfolioSeries).toHaveBeenCalledTimes(1);
    // one minute passes: no poll
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(dashboardAPI.portfolioSeries).toHaveBeenCalledTimes(1);
    // tab regains visibility (focus fires too): one refetch, data stays on screen meanwhile
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(dashboardAPI.portfolioSeries).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("portfolio-current")).toHaveTextContent("26.20 USDT");
    // a purchase action anywhere in the app
    act(() => notifyPortfolioChanged());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(dashboardAPI.portfolioSeries).toHaveBeenCalledTimes(3);
    // the 5-minute tick
    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS); });
    expect(dashboardAPI.portfolioSeries).toHaveBeenCalledTimes(4);
  });
});
