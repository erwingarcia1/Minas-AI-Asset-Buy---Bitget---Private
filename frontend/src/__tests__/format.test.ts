import { describe, expect, it } from "vitest";
import { fmtAmount, fmtCrypto } from "@/utils/format";
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
