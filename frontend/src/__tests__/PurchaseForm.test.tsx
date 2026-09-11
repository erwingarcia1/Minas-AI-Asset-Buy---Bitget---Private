import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PurchaseForm, { validateAmountUsd } from "@/components/Purchases/PurchaseForm";

vi.mock("@/api/endpoints", () => ({
  autoBuyAPI: { create: vi.fn() },
  userAPI: { exchangeKeys: vi.fn() },
}));
import { autoBuyAPI, userAPI } from "@/api/endpoints";

const KEY = { id: "k1", exchange: "binance", api_key_masked: "abcd…wxyz", verified: true, created_at: "", updated_at: "" };
const ETH = "0x52908400098527886E0F7030069857D2E4169EE7";
const BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq";
const CREATED = { purchase_id: "p1", status: "active", next_execution: null };

describe("unified purchase form (Auto-Buy / DCA)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([KEY] as never);
    vi.mocked(autoBuyAPI.create).mockResolvedValue(CREATED as never);
  });

  it("validates amount bounds", () => {
    expect(validateAmountUsd(9)).toMatch(/Minimum/);
    expect(validateAmountUsd(10)).toBeNull();
    expect(validateAmountUsd(1_000_001)).toMatch(/Maximum/);
    expect(validateAmountUsd(Number.NaN)).toMatch(/Enter an amount/);
  });

  it("shows a red X for a mismatched address and a green check for a valid one, then submits as autobuy", async () => {
    const onCreated = vi.fn();
    render(<PurchaseForm onCreated={onCreated} />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    const addr = screen.getByLabelText(/Wallet address/);
    fireEvent.change(addr, { target: { value: ETH } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✗");
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.change(addr, { target: { value: BTC } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✓");
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(CREATED));
    expect(autoBuyAPI.create).toHaveBeenCalledWith(expect.objectContaining({
      crypto_symbol: "BTC", blockchain_network: "mainnet", exchange_account_id: "k1", frequency: "weekly",
      amount_usd: 50, wallet_address: BTC, purchase_type: "autobuy", withdraw_immediately: true,
    }));
    expect(screen.getByRole("status")).toHaveTextContent(/Purchase created \(autobuy\)/);
  });

  it("unticking 'Withdraw immediately' submits DCA mode without a wallet address", async () => {
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));
    expect(screen.getByText(/Accumulate on exchange \(DCA\)/)).toBeInTheDocument();
    expect(screen.getByText(/optional/)).toBeInTheDocument();
    // no address at all: allowed in accumulate mode
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(autoBuyAPI.create).toHaveBeenCalled());
    const body = vi.mocked(autoBuyAPI.create).mock.calls[0][0];
    expect(body.purchase_type).toBe("dca");
    expect(body.withdraw_immediately).toBe(false);
    expect("wallet_address" in body).toBe(false);
  });

  it("a wallet given in accumulate mode must still be valid, and burn addresses are always rejected", async () => {
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa" } });
    expect(screen.getByText(/unspendable, burn or placeholder/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
  });

  it("withdraw mode requires a wallet address", async () => {
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.blur(screen.getByLabelText(/Wallet address/));
    expect(screen.getByText(/A wallet address is required/)).toBeInTheDocument();
  });

  it("warns and blocks when the exchange cannot withdraw to the network", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([{ ...KEY, exchange: "kraken" }] as never);
    render(<PurchaseForm />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/does not support withdrawals to mainnet/));
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    expect(autoBuyAPI.create).not.toHaveBeenCalled();
  });

  it("surfaces API validation errors", async () => {
    vi.mocked(autoBuyAPI.create).mockRejectedValue({ response: { status: 422, data: { detail: "amount_usd must be at least 10" } } });
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Could not create the purchase|amount_usd/));
  });
});
