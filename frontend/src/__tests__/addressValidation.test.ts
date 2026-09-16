import { describe, expect, it } from "vitest";
import { addressVerdict, detectChain, getAddressNetwork, isDenylisted, validateAddressForNetwork, validateBitcoinAddress, validateEthereumAddress, validateSolanaAddress } from "@/utils/addressValidation";
import { canExchangeWithdrawToNetwork } from "@/utils/exchangeNetworks";
import { addressVerdictForChain, parseStellarAddress, parseXrpAddress, tagLabelForChain, tagVerdictForChain } from "@/utils/chains";

const BTC = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy";
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

describe("burn / placeholder denylist", () => {
  it("rejects known unspendable addresses in every chain, case-insensitively for EVM", () => {
    expect(isDenylisted("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")).toBe(true);
    expect(isDenylisted("0x000000000000000000000000000000000000dEaD")).toBe(true);
    expect(isDenylisted("0x0000000000000000000000000000000000000000")).toBe(true);
    expect(isDenylisted("11111111111111111111111111111111")).toBe(true);
    expect(isDenylisted(BTC)).toBe(false);
    expect(addressVerdict("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "BTC", "mainnet")).toMatchObject({ ok: false });
    expect(addressVerdict("0x000000000000000000000000000000000000dEaD", "ETH", "mainnet")?.reason).toMatch(/burn/);
  });
});

describe("chain-based verdicts (exchange chain names)", () => {
  it("checks the address format against the chain family and refuses testnet / unsupported chains", async () => {
    const { addressVerdictForChain, familyForChain } = await import("@/utils/chains");
    expect(familyForChain("ArbitrumOne")).toBe("ethereum");
    expect(familyForChain("LIGHTNING")).toBeNull();
    expect(addressVerdictForChain(BTC, "BTC")).toMatchObject({ ok: true });
    expect(addressVerdictForChain(TESTNET, "BTC")?.reason).toMatch(/testnet/);
    expect(addressVerdictForChain(ETH, "BEP20")).toMatchObject({ ok: true });
    expect(addressVerdictForChain(BTC, "ERC20")?.reason).toMatch(/0x/);
    expect(addressVerdictForChain(SOL, "SOL")).toMatchObject({ ok: true });
    expect(addressVerdictForChain("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "TRC20")).toMatchObject({ ok: true });
    expect(addressVerdictForChain(ETH, "LIGHTNING")?.reason).toMatch(/not supported/);
    expect(addressVerdictForChain("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "BTC")?.reason).toMatch(/burn/);
  });
});

describe("XRP Ledger and Stellar addresses (tag chains)", () => {
  const XRP = "rB6d46oBHw6jfmfTnSCuCs2GAwaa25MePL";
  const XRP_X = "XVa6FLVfDCaBZAaHruwN9KDV4Rh8paJ9S75izdBzZxZnSHJ";
  const XLM = "GAWUTTOC7KHJAAM6ZTUNS6DP7MQFWKDGKTF2NCXAPQ2ZREFGDQJ2UK34";
  const XLM_M = "MBIFHKTHAKIL3RCVFJ6MWKYONQRSR5PRYVT7HHI23UHMTQXMZ3GHCAAAAAAAAAADBFFOU";

  it("parses classic and X-addresses, rejecting a one-character typo", () => {
    expect(parseXrpAddress(XRP)).toMatchObject({ ok: true, tag: null });
    expect(parseXrpAddress("rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH").ok).toBe(true);
    expect(parseXrpAddress(XRP.slice(0, -1) + "X").ok).toBe(false);
    expect(parseXrpAddress(XRP_X)).toMatchObject({ ok: true, tag: "12345", classic: "rUQL2YfQWq4qDyfbcGMiiscW5ebJNPQHyX" });
    expect(addressVerdictForChain(XRP, "XRP")).toMatchObject({ ok: true });
    expect(addressVerdictForChain(ETH, "XRP")).toMatchObject({ ok: false });
    expect(tagVerdictForChain("12345", "XRP").ok).toBe(true);
    expect(tagVerdictForChain("4294967296", "XRP").ok).toBe(false);
    expect(tagVerdictForChain("abc", "XRP").ok).toBe(false);
  });

  it("parses G and muxed M Stellar addresses and validates memos", () => {
    expect(parseStellarAddress(XLM)).toMatchObject({ ok: true, tag: null });
    expect(parseStellarAddress(XLM.slice(0, -1) + "A").ok).toBe(false);
    expect(parseStellarAddress(XLM_M)).toMatchObject({ ok: true, tag: "777", classic: "GBIFHKTHAKIL3RCVFJ6MWKYONQRSR5PRYVT7HHI23UHMTQXMZ3GHDSFC" });
    expect(addressVerdictForChain(XLM, "StellarLumens")).toMatchObject({ ok: true });
    expect(tagVerdictForChain("deposit-42", "StellarLumens").ok).toBe(true);
    expect(tagVerdictForChain("x".repeat(29), "StellarLumens").ok).toBe(false);
    expect(tagVerdictForChain("18446744073709551616", "StellarLumens").ok).toBe(false);
    expect(tagLabelForChain("XRP")).toBe("destination tag");
    expect(tagLabelForChain("StellarLumens")).toBe("memo");
    expect(tagLabelForChain("BTC")).toBeNull();
  });
});
