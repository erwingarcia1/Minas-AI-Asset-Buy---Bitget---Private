import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PurchaseForm, { validateAmountUsd } from "@/components/Purchases/PurchaseForm";

vi.mock("@/api/endpoints", () => ({
  autoBuyAPI: { create: vi.fn() },
  userAPI: { exchangeKeys: vi.fn() },
  marketAPI: { coin: vi.fn() },
}));
import { autoBuyAPI, marketAPI, userAPI } from "@/api/endpoints";

const CHAIN_BTC = { chain: "BTC", family: "bitcoin", family_label: "Bitcoin", supported: true, withdrawable: true, withdraw_fee: 0.00003,
  extra_withdraw_fee_pct: 0, min_withdraw_amount: 0.0005, withdraw_min_scale: 8, withdraw_confirm: 3, need_tag: false, browser_url: null,
  is_default: true, fee_usd: 2.31, min_purchase_usd: 41.33, estimated_received: 0.0006, meets_minimum: true };
const CHAIN_BEP20 = { ...CHAIN_BTC, chain: "BEP20", family: "ethereum", family_label: "EVM (0x…)", is_default: false, withdraw_fee: 0.00000195, min_withdraw_amount: 0.00012961, fee_usd: 0.15, min_purchase_usd: 10.5 };
const CHAIN_LIGHTNING = { ...CHAIN_BTC, chain: "LIGHTNING", family: null, family_label: null, supported: false, withdrawable: false, is_default: false };
const MARKET = { coin: "BTC", pair: "BTCUSDT", online: true, min_trade_usdt: 1, taker_fee_rate: 0.002, quantity_precision: 6, price_ask: 77042.84,
  chains: [CHAIN_BTC, CHAIN_BEP20, CHAIN_LIGHTNING], note: "" };

const KEY = { id: "k1", exchange: "binance", api_key_masked: "abcd…wxyz", verified: true, created_at: "", updated_at: "" };
const ETH = "0x52908400098527886E0F7030069857D2E4169EE7";
const BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq";
const CREATED = { purchase_id: "p1", status: "active", next_execution: null };

describe("unified purchase form (Auto-Buy / DCA)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([KEY] as never);
    vi.mocked(autoBuyAPI.create).mockResolvedValue(CREATED as never);
    vi.mocked(marketAPI.coin).mockResolvedValue(MARKET as never);
  });

  async function ready() {
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("chain-summary")).toHaveTextContent(/fee 0.00003 BTC/), { timeout: 3000 });
  }

  it("validates amount bounds", () => {
    expect(validateAmountUsd(9)).toMatch(/Minimum/);
    expect(validateAmountUsd(10)).toBeNull();
    expect(validateAmountUsd(1_000_001)).toMatch(/Maximum/);
    expect(validateAmountUsd(Number.NaN)).toMatch(/Enter an amount/);
  });

  it("shows a red X for a mismatched address and a green check for a valid one, then submits as autobuy", async () => {
    const onCreated = vi.fn();
    render(<PurchaseForm onCreated={onCreated} />);
    await ready();
    const addr = screen.getByLabelText(/Wallet address/);
    fireEvent.change(addr, { target: { value: ETH } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✗");
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.change(addr, { target: { value: BTC } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✓");
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(CREATED));
    expect(autoBuyAPI.create).toHaveBeenCalledWith(expect.objectContaining({
      crypto_symbol: "BTC", blockchain_network: "BTC", exchange_account_id: "k1", frequency: "weekly",
      amount_usd: 50, wallet_address: BTC, purchase_type: "autobuy", withdraw_immediately: true,
    }));
    expect(screen.getByRole("status")).toHaveTextContent(/Purchase created \(autobuy\)/);
  });

  it("unticking 'Withdraw immediately' submits DCA mode without a wallet address", async () => {
    render(<PurchaseForm />);
    await ready();
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
    await ready();
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa" } });
    expect(screen.getByText(/unspendable, burn or placeholder/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
  });

  it("withdraw mode requires a wallet address", async () => {
    render(<PurchaseForm />);
    await ready();
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.blur(screen.getByLabelText(/Wallet address/));
    expect(screen.getByText(/A wallet address is required/)).toBeInTheDocument();
  });

  it("shows Bitget's chains with fee and minimum, and blocks a withdraw purchase below the chain minimum", async () => {
    vi.mocked(marketAPI.coin).mockResolvedValue({ ...MARKET, chains: [{ ...CHAIN_BTC, meets_minimum: false, estimated_received: 0.00019, min_purchase_usd: 41.33 }, CHAIN_BEP20, CHAIN_LIGHTNING] } as never);
    render(<PurchaseForm />);
    await ready();
    const select = screen.getByLabelText(/Blockchain network/) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["BTC", "BEP20"]);   // unsupported LIGHTNING is not offered
    expect(screen.getByTestId("chain-summary")).toHaveTextContent(/min 0.0005 BTC \(≈ \$41.33 per buy\)/);
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/below Bitget's minimum withdrawal on BTC/));
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    expect(autoBuyAPI.create).not.toHaveBeenCalled();
    // accumulate mode does not withdraw: the minimum no longer applies
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));
    await waitFor(() => expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled());
  });

  it("offers no USDT as a purchase coin (purchases are paid in USDT)", async () => {
    render(<PurchaseForm />);
    await ready();
    const coins = Array.from((screen.getByLabelText("Crypto") as HTMLSelectElement).options).map((o) => o.value);
    expect(coins).not.toContain("USDT");
    expect(coins).toEqual(expect.arrayContaining(["BTC", "ETH", "USDC", "SOL"]));
  });

  it("accumulate mode hides the network, shows where coins are held and sends no chain", async () => {
    render(<PurchaseForm />);
    await ready();
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));
    expect(screen.queryByLabelText(/Blockchain network/)).toBeNull();
    expect(screen.getByTestId("held-on-exchange")).toHaveTextContent("Held on Binance");   // the fixture key is on Binance
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(autoBuyAPI.create).toHaveBeenCalled());
    expect(autoBuyAPI.create).toHaveBeenCalledWith(expect.not.objectContaining({ blockchain_network: expect.anything() }));
    expect(autoBuyAPI.create).toHaveBeenCalledWith(expect.objectContaining({ purchase_type: "dca" }));
  });

  it("shows a plain message, never raw exchange errors, when market data fails", async () => {
    vi.mocked(marketAPI.coin).mockRejectedValue({ response: { status: 503, data: { detail: { error: "exchange_data_unavailable",
      details: { message: "Bitget market data unavailable for bitget:market:symbol:BTCUSDT: 40034" } } } } });
    render(<PurchaseForm />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Bitget market data is temporarily unavailable. Please try again in a minute.");
    expect(alert.textContent).not.toMatch(/bitget:market|40034/);
  });

  it("validates the address against the chosen chain's format", async () => {
    render(<PurchaseForm />);
    await ready();
    fireEvent.change(screen.getByLabelText(/Blockchain network/), { target: { value: "BEP20" } });
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✗");
    expect(screen.getByText(/BEP20 needs a 0x… address/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: ETH } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✓");
  });

  it("blocks withdraw purchases when Bitget chain data is unavailable", async () => {
    vi.mocked(marketAPI.coin).mockRejectedValue(new Error("503"));
    render(<PurchaseForm />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/temporarily unavailable/));
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
  });

  it("surfaces API validation errors", async () => {
    vi.mocked(autoBuyAPI.create).mockRejectedValue({ response: { status: 422, data: { detail: "amount_usd must be at least 10" } } });
    render(<PurchaseForm />);
    await ready();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Could not create the purchase|amount_usd/));
  });
});
