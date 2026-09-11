import { describe, expect, it } from "vitest";
import { addressVerdict, detectChain, getAddressNetwork, validateAddressForNetwork, validateBitcoinAddress, validateEthereumAddress, validateSolanaAddress } from "@/utils/addressValidation";
import { canExchangeWithdrawToNetwork } from "@/utils/exchangeNetworks";

const BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy";
const SEGWIT = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", TESTNET = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn", TB1 = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx";
const ETH = "0x52908400098527886E0F7030069857D2E4169EE7", SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM";

describe("address validation (mirrors the API)", () => {
  it("bitcoin formats + checksums", () => {
    for (const a of [BTC, P2SH, SEGWIT]) expect(validateBitcoinAddress(a)).toBe(true);
    expect(validateBitcoinAddress(BTC.slice(0, -1) + "b")).toBe(false);
    expect(validateBitcoinAddress(SEGWIT.slice(0, -1) + "x")).toBe(false);
    expect(getAddressNetwork(TESTNET)).toBe("testnet");
    expect(getAddressNetwork(TB1)).toBe("testnet");
    expect(getAddressNetwork(BTC)).toBe("mainnet");
  });
  it("ethereum and solana", () => {
    expect(validateEthereumAddress(ETH)).toBe(true);
    expect(validateEthereumAddress(ETH.slice(0, -1))).toBe(false);
    expect(validateSolanaAddress(SOL)).toBe(true);
    expect(validateSolanaAddress(SOL + "A")).toBe(false);
    expect(detectChain(ETH)).toBe("ethereum"); expect(detectChain(SOL)).toBe("solana"); expect(detectChain("x")).toBeNull();
  });
  it("address must match network and coin", () => {
    expect(validateAddressForNetwork(TESTNET, "mainnet")).toBe(false);
    expect(validateAddressForNetwork(TESTNET, "testnet")).toBe(true);
    expect(validateAddressForNetwork(ETH, "testnet")).toBe(true);
    expect(addressVerdict(ETH, "BTC", "mainnet")).toMatchObject({ ok: false });
    expect(addressVerdict(ETH, "USDT", "mainnet")).toMatchObject({ ok: true });
    expect(addressVerdict(TESTNET, "BTC", "mainnet")?.reason).toMatch(/testnet/);
    expect(addressVerdict("", "BTC", "mainnet")).toBeNull();
  });
  it("exchange network support", () => {
    expect(canExchangeWithdrawToNetwork("binance", "testnet")).toBe(true);
    expect(canExchangeWithdrawToNetwork("kraken", "mainnet")).toBe(false);
  });
});
