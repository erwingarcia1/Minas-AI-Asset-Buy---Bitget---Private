import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PurchaseForm, { validateAmountUsd } from "@/components/Purchases/PurchaseForm";
import { useAuthStore } from "@/store/authStore";

vi.mock("@/api/endpoints", () => ({
  autoBuyAPI: { create: vi.fn() },
  userAPI: { exchangeKeys: vi.fn() },
  marketAPI: { coin: vi.fn(), destination: vi.fn() },
}));
import { autoBuyAPI, marketAPI, userAPI } from "@/api/endpoints";

const CHAIN_BTC = { chain: "BTC", family: "bitcoin", family_label: "Bitcoin", supported: true, withdrawable: true, withdraw_fee: 0.00003,
  extra_withdraw_fee_pct: 0, min_withdraw_amount: 0.0005, withdraw_min_scale: 8, withdraw_confirm: 3, need_tag: false, browser_url: null,
  is_default: true, fee_usd: 2.31, min_purchase_usd: 41.33, estimated_received: 0.0006, meets_minimum: true };
const CHAIN_BEP20 = { ...CHAIN_BTC, chain: "BEP20", family: "ethereum", family_label: "EVM (0x…)", is_default: false, withdraw_fee: 0.00000195, min_withdraw_amount: 0.00012961, fee_usd: 0.15, min_purchase_usd: 10.5 };
const CHAIN_LIGHTNING = { ...CHAIN_BTC, chain: "LIGHTNING", family: null, family_label: null, supported: false, withdrawable: false, is_default: false };
const MARKET = { coin: "BTC", pair: "BTCUSDT", online: true, min_trade_usdt: 1, taker_fee_rate: 0.002, quantity_precision: 6, price_ask: 77042.84,
  chains: [CHAIN_BTC, CHAIN_BEP20, CHAIN_LIGHTNING], note: "" };

const CHAIN_XRP = { ...CHAIN_BTC, chain: "XRP", family: "xrpl", family_label: "XRP Ledger", need_tag: true, tag_label: "destination tag",
  withdraw_fee: 0.2, min_withdraw_amount: 5, fee_usd: 0.5, min_purchase_usd: 14, estimated_received: 19.6 };
const MARKET_XRP = { ...MARKET, coin: "XRP", pair: "XRPUSDT", price_ask: 2.5, chains: [CHAIN_XRP] };
const XRP = "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePL";
const XRP_X = "XVa6FLVfDCaBZAaHruwN9KDV4Rh8paJ9S75izdBzZxZnSHJ";   // X-address carrying tag 12345
const KEY = { id: "k1", exchange: "binance", api_key_masked: "abcd…wxyz", verified: true, created_at: "", updated_at: "" };
const ETH = "0x52908400098527886E0F7030069857D2E4169EE7";
const BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq";
const CREATED = { purchase_id: "p1", status: "active", next_execution: null };

describe("unified purchase form (Auto-Buy / DCA)", () => {
  beforeEach(() => {
  useAuthStore.setState({ user: { id: "u1", email: "me@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", withdrawals_allowed: true } } as never);   // the withdraw toggle exists only for allowlisted users; default off
    vi.clearAllMocks();
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([KEY] as never);
    vi.mocked(autoBuyAPI.create).mockResolvedValue(CREATED as never);
    vi.mocked(marketAPI.coin).mockResolvedValue(MARKET as never);
    vi.mocked(marketAPI.destination).mockResolvedValue({ ok: false, error: "destination_type_required", message: "", needs_tag: true,
      tag_label: "destination tag", embedded_tag: null, funded: null, warning: null } as never);
  });

  async function readyXrp() {
    vi.mocked(marketAPI.coin).mockResolvedValue(MARKET_XRP as never);
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));   // these flows were written for withdraw mode; it is opt-in now
    fireEvent.change(screen.getByLabelText("Crypto"), { target: { value: "XRP" } });
    await waitFor(() => expect(screen.getByTestId("chain-summary")).toHaveTextContent(/fee 0.2 XRP/), { timeout: 3000 });
  }

  async function ready() {
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));   // withdraw mode is opt-in now
    await waitFor(() => expect(screen.getByTestId("chain-summary")).toHaveTextContent(/fee 0.00003 BTC/), { timeout: 3000 });
  }

  it("invents no minimum: only zero/negative, NaN and the ceiling are rejected client-side", () => {
    expect(validateAmountUsd(9)).toBeNull();
    expect(validateAmountUsd(0.5)).toBeNull();      // Bitget's order minimum decides, live
    expect(validateAmountUsd(0)).toMatch(/above zero/);
    expect(validateAmountUsd(-1)).toMatch(/above zero/);
    expect(validateAmountUsd(1_000_001)).toMatch(/Maximum/);
    expect(validateAmountUsd(Number.NaN)).toMatch(/Enter an amount/);
  });

  it("accumulate: $5 XRP is accepted, $0.50 is stopped by Bitget's live order minimum, never a $10 floor", async () => {
    await readyXrp();
    fireEvent.click(screen.getByLabelText(/Withdraw immediately/));   // back to accumulate
    fireEvent.change(screen.getByLabelText(/Amount/), { target: { value: "5" } });
    expect(screen.queryByText(/Minimum is 10/)).toBeNull();
    expect(screen.getByText(/No minimum beyond Bitget's 1 USDT order minimum/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled());
    fireEvent.change(screen.getByLabelText(/Amount/), { target: { value: "0.5" } });
    expect(screen.getByTestId("order-minimum")).toHaveTextContent("Bitget's minimum order for XRPUSDT is 1 USDT.");
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
  });

  it("withdraw: the chain figure carries the 1% headroom clause and $8 XRP is accepted when it clears it", async () => {
    await readyXrp();   // CHAIN_XRP: min_purchase_usd 14 → $8 below; then re-price as the live API would
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: XRP } });
    fireEvent.click(screen.getByLabelText(/A wallet I control/));
    expect(screen.getByTestId("chain-summary")).toHaveTextContent(/per buy, includes 1% headroom for the price moving between creation and execution/);
    vi.mocked(marketAPI.coin).mockResolvedValue({ ...MARKET_XRP, chains: [{ ...CHAIN_XRP, min_purchase_usd: 6.87, meets_minimum: true, estimated_received: 5.9 }] } as never);
    fireEvent.change(screen.getByLabelText(/Amount/), { target: { value: "8" } });
    await waitFor(() => expect(screen.getByTestId("chain-summary")).toHaveTextContent(/\$6.87 per buy/), { timeout: 3000 });
    expect(screen.queryByText(/Minimum is 10/)).toBeNull();
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(autoBuyAPI.create).toHaveBeenCalledWith(expect.objectContaining({ crypto_symbol: "XRP", amount_usd: 8, purchase_type: "autobuy" })));
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

  it("the default (toggle off) submits DCA mode without a wallet address", async () => {
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    expect((screen.getByLabelText(/Withdraw immediately/) as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/Accumulate on exchange \(DCA\)/)).toBeInTheDocument();
    expect(screen.getByText(/optional/)).toBeInTheDocument();
    // no address at all: allowed in accumulate mode (once the market data has loaded)
    await waitFor(() => expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled());
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
    expect(screen.getByTestId("chain-summary")).toHaveTextContent(/min 0.0005 BTC \(≈ \$41.33 per buy, includes 1% headroom/);
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
    fireEvent.click(await screen.findByLabelText(/Withdraw immediately/));   // in withdraw mode the failure is an error, not a warning
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
    fireEvent.click(await screen.findByLabelText(/Withdraw immediately/));
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

  it("XRP: asks where the address is, blocks until answered, and sends destination_type + tag for an exchange", async () => {
    await readyXrp();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: XRP } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✓");
    expect(screen.getByText("Where is this XRP address?")).toBeInTheDocument();
    expect(screen.getByText(/Sending to an exchange/)).toHaveTextContent(/without its destination tag/);
    expect(screen.getByTestId("destination-required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/An exchange or custody account/));
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeDisabled();       // tag still missing
    expect(screen.getByText(/The destination tag is required/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Destination tag"), { target: { value: "abc" } });
    expect(screen.getByText(/at most 10 digits/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Destination tag"), { target: { value: "12345" } });
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(autoBuyAPI.create).toHaveBeenCalled());
    expect(autoBuyAPI.create).toHaveBeenCalledWith(expect.objectContaining({
      crypto_symbol: "XRP", blockchain_network: "XRP", wallet_address: XRP, destination_type: "exchange", memo: "12345" }));
  });

  it("XRP: a wallet I control sends self_custody and no memo; BTC sends neither", async () => {
    await readyXrp();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: XRP } });
    fireEvent.click(screen.getByLabelText(/A wallet I control/));
    expect(screen.queryByLabelText("Destination tag")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(autoBuyAPI.create).toHaveBeenCalled());
    const body = vi.mocked(autoBuyAPI.create).mock.calls[0][0];
    expect(body).toMatchObject({ destination_type: "self_custody" });
    expect(body).not.toHaveProperty("memo");
  });

  it("XRP: an X-address selects exchange and fills the decoded tag read-only", async () => {
    await readyXrp();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: XRP_X } });
    expect(screen.getByTestId("address-indicator")).toHaveTextContent("✓");
    const tag = await screen.findByLabelText("Destination tag");
    expect(tag).toHaveValue("12345");
    expect(tag).toHaveAttribute("readonly");
    expect(screen.getByLabelText(/A wallet I control/)).toBeDisabled();
    expect(screen.getByText(/already contains destination tag 12345/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
  });

  it("XRP: warns (without blocking) when the ledger has never seen the address", async () => {
    vi.mocked(marketAPI.destination).mockResolvedValue({ ok: false, error: "destination_type_required", message: "", needs_tag: true,
      tag_label: "destination tag", embedded_tag: null, funded: false,
      warning: "This address has never received XRP. That is normal for a brand-new wallet, but a wrong character also looks like this. Double-check it before saving." } as never);
    await readyXrp();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: XRP } });
    expect(await screen.findByTestId("unfunded-warning")).toHaveTextContent(/has never received XRP/);
    fireEvent.click(screen.getByLabelText(/A wallet I control/));
    expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled();
  });

  it("BTC never shows the destination question", async () => {
    render(<PurchaseForm />);
    await ready();
    fireEvent.change(screen.getByLabelText(/Wallet address/), { target: { value: BTC } });
    expect(screen.queryByText(/Where is this/)).toBeNull();
  });
});

describe("PurchaseForm: private beta, withdrawals not allowed", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: { id: "u2", email: "tester@example.com", kyc_status: "verified", created_at: "2026-09-01T00:00:00Z", withdrawals_allowed: false } } as never);
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([KEY] as never);
    vi.mocked(autoBuyAPI.create).mockResolvedValue(CREATED as never);
    vi.mocked(marketAPI.coin).mockResolvedValue(MARKET as never);
  });

  it("hides the withdraw toggle, shows the beta line, and creates accumulate purchases", async () => {
    render(<PurchaseForm />);
    await waitFor(() => expect(userAPI.exchangeKeys).toHaveBeenCalled());
    expect(screen.queryByLabelText(/Withdraw immediately/)).toBeNull();
    expect(screen.getByTestId("withdraw-unavailable")).toHaveTextContent("Sending to your own wallet is coming later. For now, coins stay in your Bitget sub-account.");
    expect(screen.getByText(/Coins stay in your Bitget sub-account\./)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/sent to your wallet right away|add one later to switch/);
    await waitFor(() => expect(screen.getByRole("button", { name: /Create Purchase/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Create Purchase/ }));
    await waitFor(() => expect(autoBuyAPI.create).toHaveBeenCalled());
    const body = vi.mocked(autoBuyAPI.create).mock.calls[0][0];
    expect(body.purchase_type).toBe("dca");
    expect(body.withdraw_immediately).toBe(false);
  });
});

describe("PurchaseForm: fee wording", () => {
  it("states that Bitget's fee is taken from the coin received, and never a fixed percentage", async () => {
    vi.mocked(userAPI.exchangeKeys).mockResolvedValue([KEY] as never);
    vi.mocked(marketAPI.coin).mockResolvedValue(MARKET as never);
    render(<PurchaseForm />);
    await waitFor(() => expect(screen.getByText(/Bitget's trading fee applies, taken from the coin you receive/)).toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/taker fee|0\.20%/);
  });
});
