import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PurchaseForm, { validateAmount } from "../components/Purchases/PurchaseForm";

vi.mock("../services/api", () => ({
  purchases: { create: vi.fn(), update: vi.fn() },
}));
import { purchases } from "../services/api";

describe("validateAmount", () => {
  it("enforces the 10..1,000,000 USDT bounds", () => {
    expect(validateAmount(9.99)).toMatch(/Minimum/);
    expect(validateAmount(10)).toBeNull();
    expect(validateAmount(1_000_000)).toBeNull();
    expect(validateAmount(1_000_001)).toMatch(/Maximum/);
    expect(validateAmount(NaN)).toMatch(/Enter/);
  });
});

describe("PurchaseForm", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("submits a valid schedule", async () => {
    vi.mocked(purchases.create).mockResolvedValue({ id: "p1" } as never);
    const onSaved = vi.fn();
    render(<PurchaseForm onSaved={onSaved} onCancel={() => undefined} />);
    fireEvent.change(screen.getByLabelText(/Amount/), { target: { value: "250" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(purchases.create).toHaveBeenCalledWith(
      expect.objectContaining({ crypto: "BTC", amount_usdt: 250, frequency: "weekly" }));
  });

  it("blocks submission below the minimum and shows the reason", () => {
    render(<PurchaseForm onSaved={() => undefined} onCancel={() => undefined} />);
    fireEvent.change(screen.getByLabelText(/Amount/), { target: { value: "5" } });
    expect(screen.getByText(/Minimum is 10 USDT/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create schedule" })).toBeDisabled();
    expect(purchases.create).not.toHaveBeenCalled();
  });

  it("surfaces API validation errors", async () => {
    vi.mocked(purchases.create).mockRejectedValue(
      { response: { data: { detail: "An active BTC schedule already exists" } } });
    render(<PurchaseForm onSaved={() => undefined} onCancel={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("already exists"));
  });
});
