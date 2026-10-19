import { fireEvent, render, screen } from "@testing-library/react";
import { existsSync } from "node:fs";
import path from "node:path";
import { CoinLogo } from "@/components/Common/CoinLogo";
import { ASSET_COLORS } from "@/utils/assetColors";
import { COIN_CATEGORIES } from "@/utils/exchangeNetworks";
import { LOGO_ENTRIES, logoEntry, logoPath } from "@/utils/logoSources";

// vitest runs from frontend/ (locally and in CI); walk up in case it is started from the repo root
function logosDir(): string {
  let dir = process.cwd();
  for (let i = 0; i < 4; i++) {
    const candidate = path.join(dir, "public", "logos");
    if (existsSync(path.join(candidate, "LOGO_SOURCES.md"))) return candidate;
    const fe = path.join(dir, "frontend", "public", "logos");
    if (existsSync(path.join(fe, "LOGO_SOURCES.md"))) return fe;
    dir = path.dirname(dir);
  }
  throw new Error("public/logos not found");
}
const LOGOS_DIR = logosDir();
const SUPPORTED = COIN_CATEGORIES.flatMap((g) => g.coins.map((c) => c.symbol));

describe("LOGO_SOURCES.md", () => {
  it("lists every supported coin exactly once", () => {
    const listed = LOGO_ENTRIES.map((e) => e.symbol);
    for (const s of SUPPORTED) expect(listed.filter((x) => x === s)).toHaveLength(1);
  });

  it("every coin with a logo has its file in public/logos, with a shape and a source", () => {
    for (const e of LOGO_ENTRIES.filter((e) => e.file)) {
      expect(existsSync(path.join(LOGOS_DIR, e.file)), `${e.symbol}: public/logos/${e.file} is missing`).toBe(true);
      expect(["circle", "mark"]).toContain(e.shape);
      expect(e.source).not.toBe("");
      expect(e.licence).not.toBe("");
    }
  });

  it("no logo is loaded from outside our own domain", () => {
    for (const e of LOGO_ENTRIES) {
      expect(e.file).not.toMatch(/:\/\/|^\/|\.\./);
      const p = logoPath(e.symbol);
      if (p) expect(p).toMatch(/^\/logos\/[A-Za-z0-9._-]+$/);
    }
  });

  it("all ten supported coins have a logo file", () => {
    expect(LOGO_ENTRIES.filter((e) => e.file).map((e) => e.symbol).sort()).toEqual([...SUPPORTED].sort());
    expect(LOGO_ENTRIES.filter((e) => !e.file)).toEqual([]);
    expect(logoEntry("eth")?.notice).toMatch(/CC BY 4\.0/);
    for (const s of ["XLM", "SHIB", "PEPE", "BONK"]) expect(logoEntry(s)?.source).toMatch(/^Icon as displayed on Bitget \(https:\/\/img\.bgstatic\.com\/.+\); trademark of .+; used unaltered only to identify the coin$/);
  });
});

describe("CoinLogo", () => {
  it("shows the logo from /logos/ on a plate, decorative, at the requested size", () => {
    render(<CoinLogo symbol="BTC" size={32} />);
    const img = screen.getByTestId("coin-logo").querySelector("img")!;
    expect(img).toHaveAttribute("src", "/logos/btc.svg");
    expect(img).toHaveAttribute("alt", "");
    expect(screen.getByTestId("coin-logo")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByTestId("coin-logo")).toHaveStyle({ width: "32px", height: "32px" });
  });

  it("a mark-shaped logo sits inside the plate; a circle fills it", () => {
    const { unmount } = render(<CoinLogo symbol="ETH" size={32} />);
    expect(screen.getByTestId("coin-logo").querySelector("img")).toHaveAttribute("width", "22");
    unmount();
    render(<CoinLogo symbol="USDC" size={32} />);
    expect(screen.getByTestId("coin-logo").querySelector("img")).toHaveAttribute("width", "32");
  });

  it("falls back to the letter circle when the file fails to load", () => {
    render(<CoinLogo symbol="DOGE" size={28} />);
    fireEvent.error(screen.getByTestId("coin-logo").querySelector("img")!);
    expect(screen.queryByTestId("coin-logo")).toBeNull();
    const el = screen.getByTestId("coin-letter");
    expect(el).toHaveAttribute("data-letter", "D");
    expect(el).toHaveStyle({ width: "28px", background: ASSET_COLORS.DOGE });
  });

  it("an unknown symbol gets a letter circle, never a request", () => {
    render(<CoinLogo symbol="ZZZ" />);
    expect(screen.getByTestId("coin-letter")).toHaveAttribute("data-letter", "Z");
    expect(document.querySelector("img")).toBeNull();
  });
});
