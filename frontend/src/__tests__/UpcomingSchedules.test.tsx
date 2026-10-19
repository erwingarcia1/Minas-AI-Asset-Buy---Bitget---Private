import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import UpcomingSchedules from "@/components/Transactions/UpcomingSchedules";

vi.mock("@/api/endpoints", () => ({ autoBuyAPI: { list: vi.fn() } }));
import { autoBuyAPI } from "@/api/endpoints";

const base = { wallet_address: null, blockchain_network: null, exchange_account_id: "k1", exchange: "bitget",
  failure_count: 0, last_error: null };
const weekly = { ...base, id: "p1", crypto_symbol: "XRP", amount_usd: 15, frequency: "weekly", status: "active",
  next_execution: "2026-10-19T00:16:57Z" };
const oneTime = { ...base, id: "p2", crypto_symbol: "BTC", amount_usd: 50, frequency: "one_time", status: "active",
  next_execution: "2026-10-15T14:00:00Z" };
const paused = { ...base, id: "p3", crypto_symbol: "ETH", amount_usd: 20, frequency: "daily", status: "paused",
  next_execution: "2026-10-15T09:00:00Z" };
const done = { ...base, id: "p4", crypto_symbol: "PEPE", amount_usd: 2, frequency: "one_time", status: "completed",
  next_execution: null };

describe("UpcomingSchedules on the Transactions page", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists only active schedules with a next run, soonest first, times in ET with UTC in brackets", async () => {
    vi.mocked(autoBuyAPI.list).mockResolvedValue([weekly, oneTime, paused, done] as never);
    render(<MemoryRouter><UpcomingSchedules /></MemoryRouter>);
    const rows = await screen.findAllByTestId("upcoming-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("BTC");
    expect(rows[0]).toHaveTextContent("50.00 USDT");
    expect(rows[0]).toHaveTextContent("One-time");
    expect(rows[1]).toHaveTextContent("XRP");
    expect(rows[1]).toHaveTextContent("Weekly");
    expect(rows[1]).toHaveTextContent(/at 8:16 PM ET \(00:16 UTC\)/); // Sunday 00:16 UTC = Saturday evening Eastern
    expect(screen.queryByText("ETH")).toBeNull();
    expect(screen.queryByText("PEPE")).toBeNull();
    expect(screen.getByText(/These run automatically/)).toBeInTheDocument();
  });

  it("empty state points at creating a schedule", async () => {
    vi.mocked(autoBuyAPI.list).mockResolvedValue([done] as never);
    render(<MemoryRouter><UpcomingSchedules /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId("upcoming-schedules")).toHaveTextContent(/Nothing scheduled/));
    expect(screen.queryAllByTestId("upcoming-row")).toHaveLength(0);
  });
});
