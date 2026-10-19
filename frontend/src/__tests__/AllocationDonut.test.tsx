import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AllocationDonut, { buildSlices } from "@/components/Dashboard/AllocationDonut";
import type { PerformanceHolding } from "@/types";

const h = (crypto: string, total_amount: number, invested_usdt: number, market_value: number | null): PerformanceHolding =>
  ({ crypto, total_amount, invested_usdt, average_buy_price: invested_usdt / total_amount, market_value, current_price: market_value == null ? null : market_value / total_amount });

const three = [h("BTC", 0.000196803, 15, 15.2), h("XLM", 33.28668, 7, 7.1), h("XRP", 1.3320666, 4, 3.9)];

describe("buildSlices", () => {
  it("shares by current value by default and by cost on the toggle, quantities from the acquired figures", () => {
    const v = buildSlices(three, "value");
    expect(v.total).toBeCloseTo(26.2);
    expect(v.slices.map((s) => s.name)).toEqual(["BTC", "XLM", "XRP"]);
    expect(v.slices[0].pct).toBeCloseTo(58.02, 1);
    expect(v.slices[0].amount).toBe(0.000196803);
    const c = buildSlices(three, "cost");
    expect(c.total).toBe(26);
    expect(c.slices[2]).toMatchObject({ name: "XRP", usdt: 4 });
    expect(c.slices[2].pct).toBeCloseTo(15.38, 1);
  });

  it("collapses everything beyond six assets into Other", () => {
    const many = ["A", "B", "C", "D", "E", "F", "G", "H"].map((n, i) => h(n, 1, 100 - i * 10, 100 - i * 10));
    const { slices } = buildSlices(many, "value");
    expect(slices.map((s) => s.name)).toEqual(["A", "B", "C", "D", "E", "Other"]);
    expect(slices[5]).toMatchObject({ usdt: 50 + 40 + 30, members: ["F", "G", "H"] });
    expect(buildSlices(many.slice(0, 6), "value").slices.map((s) => s.name)).not.toContain("Other");
  });

  it("drops assets with nothing to show in the chosen basis", () => {
    expect(buildSlices([h("BTC", 0.001, 50, null), h("ETH", 1, 30, 31)], "value").slices.map((s) => s.name)).toEqual(["ETH"]);
  });
});

describe("AllocationDonut", () => {
  it("shows the market-value total in the centre, a legend with text labels, and asset details on hover/focus", () => {
    render(<AllocationDonut holdings={three} priceAvailable totalMarketValue={26.2} />);
    expect(screen.getByText("Allocation by current value")).toBeInTheDocument();
    expect(screen.getByTestId("donut-total")).toHaveTextContent("26.20 USDT");
    expect(screen.getByRole("button", { name: "Current value" })).toHaveAttribute("aria-pressed", "true");
    const btc = screen.getByRole("button", { name: /^BTC: 58%, 15\.20 USDT, 0\.0001968 BTC$/ });
    fireEvent.focus(btc);
    expect(screen.getByTestId("donut-centre")).toHaveTextContent("0.0001968 BTC");
    expect(screen.getByTestId("donut-centre")).toHaveTextContent("58% of value");
    fireEvent.blur(btc);
    expect(screen.getByTestId("donut-total")).toHaveTextContent("26.20 USDT");
  });

  it("toggles to cost and labels it", () => {
    render(<AllocationDonut holdings={three} priceAvailable totalMarketValue={26.2} />);
    fireEvent.click(screen.getByRole("button", { name: "Cost" }));
    expect(screen.getByText("Allocation by cost")).toBeInTheDocument();
    expect(screen.getByTestId("donut-total")).toHaveTextContent("26.00 USDT");
    expect(screen.getByRole("button", { name: /^XRP: 15\.4%, 4\.00 USDT/ })).toBeInTheDocument();
  });

  it("falls back to cost when live prices are unavailable", () => {
    render(<AllocationDonut holdings={three.map((x) => ({ ...x, market_value: null, current_price: null }))} priceAvailable={false} totalMarketValue={null} />);
    expect(screen.getByText("Allocation by cost")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Current value" })).toBeDisabled();
    expect(screen.getByTestId("donut-total")).toHaveTextContent("26.00 USDT");
  });

  it("has an empty state", () => {
    render(<AllocationDonut holdings={[]} priceAvailable totalMarketValue={0} />);
    expect(screen.getByTestId("allocation-donut")).toHaveTextContent("No completed purchases yet.");
  });
});
