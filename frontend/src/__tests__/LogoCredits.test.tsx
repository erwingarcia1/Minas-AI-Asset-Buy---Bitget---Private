import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import LogoCredits from "@/pages/LogoCredits";
import { LOGO_ENTRIES } from "@/utils/logoSources";

describe("Logo credits page", () => {
  it("lists every coin that displays a logo (all ten), the ETH attribution and the trademark line; no 'shown with their initial' line is left", () => {
    render(<MemoryRouter><LogoCredits /></MemoryRouter>);
    expect(screen.getAllByTestId(/^credit-/)).toHaveLength(10);
    expect(screen.queryByTestId("pending-logos")).toBeNull();   // every coin has a file, so the line is gone on its own
    expect(screen.getByTestId("trademark-line")).toHaveTextContent("All coin logos are trademarks of their respective owners and are used only to identify each coin.");
    for (const e of LOGO_ENTRIES.filter((e) => e.file)) {
      const row = screen.getByTestId(`credit-${e.symbol}`);
      expect(row).toHaveTextContent(e.symbol);
      expect(row).toHaveTextContent(e.source.slice(0, 20));
      if (e.notice) expect(screen.getByTestId(`notice-${e.symbol}`)).toHaveTextContent(e.notice);
    }
    expect(screen.getByTestId("notice-ETH")).toHaveTextContent("CC BY 4.0");
    expect(screen.queryByTestId("notice-XLM")).toBeNull();   // Stellar's notice appears once its row carries one
    for (const a of screen.getAllByRole("link", { name: /./ })) {
      const href = a.getAttribute("href") ?? "";
      if (href.startsWith("http")) expect(a).toHaveAttribute("rel", "noopener noreferrer");
    }
  });
});
