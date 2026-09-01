import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PurchaseList from "../components/Purchases/PurchaseList";

vi.mock("../services/api", () => ({
  purchases: { list: vi.fn(), toggle: vi.fn(), remove: vi.fn(),
               create: vi.fn(), update: vi.fn() },
}));
import { purchases } from "../services/api";

const row = { id: "p1", crypto: "ETH", amount_usdt: 50, frequency: "daily",
              next_purchase_date: "2026-09-02T00:00:00Z", enabled: true,
              withdrawal_address: null, created_at: "", updated_at: "" };

describe("PurchaseList", () => {
  it("renders schedules and toggles state", async () => {
    vi.mocked(purchases.list).mockResolvedValue([row] as never);
    vi.mocked(purchases.toggle).mockResolvedValue({ ...row, enabled: false } as never);
    render(<PurchaseList />);
    await waitFor(() => expect(screen.getByText("ETH")).toBeInTheDocument());
    expect(screen.getByText("enabled")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(screen.getByText("paused")).toBeInTheDocument());
  });

  it("shows the empty state", async () => {
    vi.mocked(purchases.list).mockResolvedValue([] as never);
    render(<PurchaseList />);
    await waitFor(() =>
      expect(screen.getByText(/No schedules yet/)).toBeInTheDocument());
  });
});
