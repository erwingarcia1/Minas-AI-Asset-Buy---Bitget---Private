import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ASSET_COLORS } from "@/utils/assetColors";

/** The one-line path: clearing a coin's File cell in LOGO_SOURCES.md (here: XLM) puts the letter
 *  circle back, drops the coin from the credits table and names it in the "shown with its initial" line. */
vi.mock("@/utils/logoSources", async () => {
  const real = await vi.importActual<typeof import("@/utils/logoSources")>("@/utils/logoSources");
  const LOGO_ENTRIES = real.LOGO_ENTRIES.map((e) => (e.symbol === "XLM" ? { ...e, file: "", shape: "" as const } : e));
  const logoEntry = (s: string) => LOGO_ENTRIES.find((e) => e.symbol === s.toUpperCase());
  const logoPath = (s: string) => { const e = logoEntry(s); return e && e.file ? `/logos/${e.file}` : null; };
  return { LOGO_ENTRIES, logoEntry, logoPath };
});

describe("removing a logo is one line in LOGO_SOURCES.md", () => {
  it("the coin shows the letter circle again, in its colour, with no image request", async () => {
    const { CoinLogo } = await import("@/components/Common/CoinLogo");
    render(<CoinLogo symbol="XLM" size={32} />);
    const el = screen.getByTestId("coin-letter");
    expect(el).toHaveAttribute("data-letter", "X");
    expect(el).toHaveStyle({ width: "32px", background: ASSET_COLORS.XLM });
    expect(document.querySelector("img")).toBeNull();
  });

  it("the credits page drops its row and names it in the initial line (singular wording)", async () => {
    const { default: LogoCredits } = await import("@/pages/LogoCredits");
    render(<MemoryRouter><LogoCredits /></MemoryRouter>);
    expect(screen.queryByTestId("credit-XLM")).toBeNull();
    expect(screen.getAllByTestId(/^credit-/)).toHaveLength(9);
    expect(screen.getByTestId("pending-logos")).toHaveTextContent("XLM is shown with its initial until an official logo is available.");
    expect(screen.getByTestId("trademark-line")).toBeInTheDocument();
  });
});
