import { describe, expect, it } from "vitest";
import { fmtAmount, fmtCrypto, fmtPrice } from "@/utils/format";
import { fmtCoin } from "@/utils/chains";

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
    expect(fmtCoin(0.0005, "BTC")).toBe("0.0005 BTC");
  });
});

describe("fmtPrice follows the pair's precision", () => {
  it("keeps sub-dollar significant figures and BTC's two decimals", () => {
    expect(fmtPrice(0.20945, 5)).toBe("0.20945 USDT");
    expect(fmtPrice(1.4999, 4)).toBe("1.4999 USDT");
    expect(fmtPrice(76132.62, 2)).toBe("76,132.62 USDT");
    expect(fmtPrice(null, 2)).toBe("—");
  });
});
