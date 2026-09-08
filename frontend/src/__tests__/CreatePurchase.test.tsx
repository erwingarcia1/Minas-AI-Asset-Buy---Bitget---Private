import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreatePurchase, { validateAmountUsd } from "../components/AutoBuy/CreatePurchase";

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return { ...actual, autoBuy: { create: vi.fn() }, exchange: { listKeys: vi.fn() } };
});
import { autoBuy, exchange } from "../services/api";

const KEY = { id: "k1", exchange: "binance", api_key_masked: "abcd…wxyz", verified: true, created_at: "", updated_at: "" };

describe("CreatePurchase", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(exchange.listKeys).mockResolvedValue([KEY]); });

  it("validates amount bounds", () => {
    expect(validateAmountUsd(9)).toMatch(/Minimum/); expect(validateAmountUsd(10)).toBeNull(); expect(validateAmountUsd(1_000_001)).toMatch(/Maximum/);
  });

  it("shows a red X for a mismatched address and a green check for a valid one, then submits", async () => {
    vi.mocked(autoBuy.create).mockResolvedValue({ purchase_id: "p1", status: "active", next_execution: null });
    const onCreated = vi.fn();
    render(<CreatePurchase onCreated={onCreated} />);
    await waitFor(() => expect(exchange.listKeys).toHaveBeenCalled());
    const addr = screen.getByLabelText(/Wallet address/);
    fireEvent.change(addr, { target: { value: "0x52908400098527886E0F7030069857D2E4169EE7" } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✗");
    expect(screen.getByRole("button", { name: /Create auto-buy/ })).toBeDisabled();
    fireEvent.change(addr, { target: { value: "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq" } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✓");
    fireEvent.click(screen.getByRole("button", { name: /Create auto-buy/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(autoBuy.create).toHaveBeenCalledWith(expect.objectContaining({ crypto_symbol: "BTC", blockchain_network: "mainnet", exchange_account_id: "k1", frequency: "weekly", amount_usd: 50 }));
  });

  it("warns when the exchange cannot withdraw to the network", async () => {
    vi.mocked(exchange.listKeys).mockResolvedValue([{ ...KEY, exchange: "kraken" }]);
    render(<CreatePurchase onCreated={() => undefined} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/cannot withdraw/));
  });
});
