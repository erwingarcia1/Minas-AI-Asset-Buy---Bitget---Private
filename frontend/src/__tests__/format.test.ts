import { describe, expect, it } from "vitest";
import { fmtAmount, fmtCrypto, fmtPrice, fmtUtc } from "@/utils/format";

describe("crypto amount formatting", () => {
  it("shows at most 8 decimals and trims trailing zeros, never exponent notation", () => {
    expect(fmtAmount(0.00076179)).toBe("0.00076179");
    expect(fmtAmount(0.0000015)).toBe("0.0000015");
    expect(fmtAmount(0.00003)).toBe("0.00003");
    expect(fmtAmount(0.5)).toBe("0.5");
    expect(fmtAmount(2)).toBe("2");
    expect(fmtAmount(0)).toBe("0");
    expect(fmtAmount(0.123456789123)).toBe("0.12345679");
    expect(fmtCrypto(0.00100000, "BTC")).toBe("0.001 BTC");
    expect(fmtCrypto(null)).toBe("—");
    expect(fmtCrypto(0.0005, "BTC")).toBe("0.0005 BTC");
  });
});

describe("fmtPrice: at least 4 significant digits, never fewer than 2 decimals, separators", () => {
  it("formats BTC, sub-dollar and sub-cent prices", () => {
    expect(fmtPrice(84487.36)).toBe("84,487.36 USDT");
    expect(fmtPrice(0.09179)).toBe("0.09179 USDT");
    expect(fmtPrice(0.2407)).toBe("0.2407 USDT");
    expect(fmtPrice(0.000005621)).toBe("0.000005621 USDT");
    expect(fmtPrice(0.000004181)).toBe("0.000004181 USDT");
    expect(fmtPrice(1)).toBe("1.00 USDT");
    expect(fmtPrice(2345.678)).toBe("2,345.68 USDT");
    expect(fmtPrice(null)).toBe("—");
    expect(fmtPrice(0.000000012)).not.toMatch(/e/);
  });
});

describe("fmtAmount: separators, at most 2 decimals from 1,000 coins", () => {
  it("formats large meme-coin amounts and small BTC amounts", () => {
    expect(fmtAmount(2389380.228)).toBe("2,389,380.23");
    expect(fmtAmount(1778330.9)).toBe("1,778,330.9");
    expect(fmtAmount(1000)).toBe("1,000");
    expect(fmtAmount(999.99999999)).toBe("999.99999999");
    expect(fmtAmount(0.000196803)).toBe("0.0001968");
    expect(fmtCrypto(2391772, "PEPE")).toBe("2,391,772 PEPE");
  });
});

describe("fmtUtc: Guardian times on a fixed UTC clock that says so", () => {
  it("formats zoned stamps, takes a bare stamp as UTC, and dashes for nothing", () => {
    expect(fmtUtc("2026-10-19T14:03:27Z")).toBe("2026-10-19 14:03 UTC");
    expect(fmtUtc("2026-10-19T14:03:27+00:00")).toBe("2026-10-19 14:03 UTC");
    expect(fmtUtc("2026-10-19T16:03:27+02:00")).toBe("2026-10-19 14:03 UTC");
    expect(fmtUtc("2026-10-19T14:03:27")).toBe("2026-10-19 14:03 UTC");           // the engine writes UTC without a zone
    expect(fmtUtc("2026-10-19T14:03:27.123456Z")).toBe("2026-10-19 14:03 UTC");
    expect(fmtUtc("2026-01-05T03:07:00Z")).toBe("2026-01-05 03:07 UTC");           // zero-padded
    expect(fmtUtc(null)).toBe("—");
    expect(fmtUtc(undefined)).toBe("—");
    expect(fmtUtc("not a date")).toBe("—");
  });
});
